import ast
import json
import re
from functools import lru_cache
from typing import TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from app.agent.llm import planner
from app.agent.plan import PlanError, validate
from app.services import tools as tool_service
from app.tools import SPECS, UnknownTool, label_of, spec_of

_SYSTEM = """你是电商图片修图助手，通过调用工具完成用户的修图请求。

规则：
- 只能使用已提供的工具。可以一次安排多步，按完成先后调用；后一步默认依赖前一步。
- 能一步完成的不要拆成多步，最多 8 步。
- 缺失参数用画布信息与常识补齐，可推断的参数不要反问用户。
- 图层与选区是两个独立维度：layer_id 决定改哪一层，选区决定改哪块区域。
  用户点名了图层（如「物体2」「背景」「主体」）就把它的 id 或名字填进 layer_id。
- 画布摘要标明已有选区时，那就是用户要改的物体：局部消除、替换、提升为图层
  直接调用，不要再让用户重选，也不要猜测选区是否点准。没有选区也能调用，
  此时作用在整个目标图层。
- 一句话要改多个图层时，按图层拆成多步，每步只填一个 layer_id。
- 改颜色、换材质、换成某物用 replace_region 并点名 layer_id；
  只改整张背景才用 replace_background。adjust_image 只做明暗冷暖，不做改色。
- 翻转画面用 flip_layer；说了角度用 rotate_layer。
- 用户要求拆层、把物体独立成层时，使用 split_layers 或 promote_object_to_layer。
- 拆层默认不拆文字，只有用户明确要求时才传 include_text。
- 改文字图层的文案、字号或颜色用 set_layer_text，并点名图层。
- 出营销图、主图、场景图、模特图、海报用 generate_marketing，
  kind 分别为 product / scene / model / poster。结果只进图片墙。
- 改尺寸、出投放物料、出 1:1 / 4:5 / 9:16 用 prepare_delivery_sizes，
  不要裁切主体，也不要用 crop_canvas。
- 对多张图做同一套去背景、换背景、调色、超分、扩图或改尺寸时用 batch_process。
  单张精修、营销图、局部编辑和拆层不要用它。
- 调用工具时不要输出解释或工具名。只有指令与修图无关、或现有工具确实做不到时，
  才用一句中文说明，不要提内部参数。

当前画布：{context}"""

_FALLBACK_REPLY = "没太理解这条指令，换个说法或说得更具体一些。"
_REFUSAL_LIMIT = 60
_CALL_LIKE = re.compile(r"^[a-z_][a-z0-9_]*\s*\(")
_TOOL_USE_BLOCK = re.compile(r'^\[\s*\{\s*"type"\s*:\s*"tool_use"')
_TAG_LIKE = re.compile(r"^\[[a-z]+(:[^\]]*)?\]$")
_TOOL_CALL_TAG = re.compile(r"\[/?TOOL_CALL\]", re.IGNORECASE)
_TOOL_CALL_SEGMENT = re.compile(r"\[TOOL_CALL\](.*?)\[/TOOL_CALL\]", re.DOTALL | re.IGNORECASE)


@lru_cache
def _registered_call_pattern() -> re.Pattern[str]:
    names = "|".join(re.escape(spec.name) for spec in SPECS)
    return re.compile(rf"\b(?:{names})\s*\(")


class AgentState(TypedDict):
    goal: str
    context: str
    plan: list[dict]
    reply: str


async def _plan(state: AgentState) -> AgentState:
    message = await planner().ainvoke(
        [
            SystemMessage(_SYSTEM.format(context=state["context"])),
            HumanMessage(state["goal"]),
        ]
    )
    calls = message.tool_calls
    reply = _text_of(message)
    if not calls:
        # 模型偶发把调用写进正文而不走结构化通道；整段恰为一个裸调用时恢复其意图
        recovered = _recover_tool_calls(reply)
        if recovered is not None:
            calls, reply = recovered, ""
    return {
        "plan": [{"tool": call["name"], "params": call["args"]} for call in calls],
        "reply": reply,
    }


def _verify(state: AgentState) -> AgentState:
    """模型给出的计划一律经服务端校验，不可直接执行。"""
    try:
        return {"plan": validate(state["plan"])}
    except (UnknownTool, tool_service.InvalidParams, PlanError) as exc:
        return {"plan": [], "reply": f"这一步暂时执行不了：{exc}"}


@lru_cache
def _graph():
    builder = StateGraph(AgentState)
    builder.add_node("plan", _plan)
    builder.add_node("verify", _verify)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "verify")
    builder.add_edge("verify", END)
    return builder.compile()


async def run(goal: str, context: str) -> tuple[str, list[dict]]:
    """规划并校验一轮指令，返回答复与尚未下发的计划。"""
    state = await _graph().ainvoke({"goal": goal, "context": context, "plan": [], "reply": ""})
    return spoken(state["reply"], state["plan"]), state["plan"]


def spoken(reply: str, plan: list[dict]) -> str:
    """有计划就按步骤说话；没计划才用兜底或截成一句拒绝。"""
    text = (reply or "").strip()
    if plan:
        if text and text != _FALLBACK_REPLY and not _looks_internal(text):
            return text
        labels = "、".join(label_of(step["tool"], step.get("params")) for step in plan)
        if len(plan) > 1:
            return f"将按以下步骤执行：{labels}。确认后开始。"
        return f"好，正在{labels}。"
    if _looks_internal(text):
        return _FALLBACK_REPLY
    return _one_sentence(text) if text else _FALLBACK_REPLY


def _one_sentence(text: str) -> str:
    for sep in ("。", "！", "？", "\n"):
        head, found, _ = text.partition(sep)
        if found and head.strip():
            return head.strip() + (sep if sep != "\n" else "。")
    if len(text) > _REFUSAL_LIMIT:
        return text[:_REFUSAL_LIMIT].rstrip() + "…"
    return text


def _text_of(message: AIMessage) -> str:
    if isinstance(message.content, str):
        return message.content.strip()
    return "".join(
        block.get("text", "") for block in message.content if isinstance(block, dict)
    ).strip()


def _looks_like_call(text: str) -> bool:
    return _CALL_LIKE.match((text or "").strip()) is not None


def _looks_internal(text: str) -> bool:
    """裸调用签名、tool_use JSON 块、[TOOL_CALL] 标签、[mode: …] 一类内部格式都不该说给用户。"""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped[0] in "[{":
        try:
            json.loads(stripped)
            return True  # 整段合法 JSON 是结构化数据，不是自然语言
        except ValueError:
            pass
    return bool(
        _CALL_LIKE.match(stripped)
        or _TOOL_USE_BLOCK.match(stripped)
        or _TAG_LIKE.match(stripped)
        or _TOOL_CALL_TAG.search(stripped)
        or _registered_call_pattern().search(stripped)
    )


def _recover_tool_calls(text: str) -> list[dict] | None:
    """从正文恢复模型没走结构化通道的工具调用，恢复不了返回 None。

    只认三种整段形态：`tool(prompt="…")` 裸调用、`[{"type":"tool_use",…}]` JSON 块、
    `[TOOL_CALL]…[/TOOL_CALL]` 标签。ast.parse/json.loads 都不执行代码，
    参数值逐个过 literal_eval 只允许字面量；工具名必须命中注册表，
    恢复出的计划照常走 validate() 校验。
    """
    stripped = (text or "").strip()
    segments = _TOOL_CALL_SEGMENT.findall(stripped)
    if segments:
        if _TOOL_CALL_SEGMENT.sub("", stripped).strip():
            return None  # 标签外还有别的话，不是纯泄漏
        calls = []
        for inner in segments:
            recovered = _recover_one(inner.strip())
            if recovered is None:
                return None
            calls.extend(recovered)
        return calls or None
    if _looks_like_call(stripped) or stripped.startswith(("{", "[")):
        return _recover_one(stripped)
    return None


def _recover_one(inner: str) -> list[dict] | None:
    """恢复单段调用：JSON 块、`name(args)` 裸调用或光杆工具名。"""
    if inner.startswith(("[", "{")):
        try:
            parsed = json.loads(inner)
        except ValueError:
            return None
        blocks = parsed if isinstance(parsed, list) else [parsed]
        calls = [_recover_block(block) for block in blocks]
        if any(call is None for call in calls) or not calls:
            return None
        return calls
    try:
        call = ast.parse(inner, mode="eval").body
    except SyntaxError:
        return None
    if isinstance(call, ast.Name):  # 光杆工具名，参数缺失交给 validate() 拦截
        try:
            spec_of(call.id)
        except UnknownTool:
            return None
        return [{"name": call.id, "args": {}}]
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and not call.args  # 位置参数含义不明，不猜
        and all(keyword.arg is not None for keyword in call.keywords)
    ):
        return None
    try:
        params = {keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords}
    except (ValueError, SyntaxError):
        return None
    try:
        spec_of(call.func.id)
    except UnknownTool:
        return None
    return [{"name": call.func.id, "args": params}]


def _recover_block(block: object) -> dict | None:
    if not isinstance(block, dict):
        return None
    name = block.get("name")
    params = block.get("input", block.get("args", block.get("arguments")))
    function = block.get("function")
    if isinstance(function, dict):  # OpenAI 风格 {"function": {"name":…, "arguments":…}}
        name = function.get("name") or name
        params = function.get("arguments", params)
    if isinstance(params, str):  # OpenAI 的 arguments 常是 JSON 字符串
        try:
            params = json.loads(params)
        except ValueError:
            return None
    if not isinstance(name, str) or not isinstance(params, dict):
        return None
    try:
        spec_of(name)
    except UnknownTool:
        return None
    return {"name": name, "args": params}
