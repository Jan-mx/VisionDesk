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
- 摘要里的「当前选中图层」是用户在界面点选的层。用户说「这层、选中这层」
  时把它的 id 填进 layer_id；但用户点名了另一层时以点名为准，
  有选区的区域工具仍按选区命中，不要因为选中层而改目标。
- 摘要里的「最近对话」是上一轮的目标与结果。用户的续句（如「再来一份」「并要…」
  ）沿用上轮的主体、颜色与输出方式，除非明确改变意图；
  只有用户明确要求「从零生成/全新的画面」时才用 generate_image。
- 用户要求同一物体/商品出多种颜色、每色各一个主体（如「红蓝黑各一只」）时，
  必须用 create_color_variants 并在 colors 里列出全部颜色，一步完成全部颜色。
  画布未拆层且无选区时，第一步 split_layers，第二步 create_color_variants，
  不要在这两步之外再排其他改色步骤。
  不要用 generate_image 从零生成，不要用 replace_region 逐个改色，
  也不要把多个颜色合成一张拼图交付。
- 一句话里有多项要求（先…再…、…后…、并且…）时，把每一项都排成步骤一次给出，
  不要只排第一项；后续步骤用到前面产物时加 from_previous=true。
- 用户要对「刚生成的海报/上一张」继续处理（出尺寸、改文字等）时，
  后一步工具加 from_previous=true 消费上一步产物。跨轮也一样：
  摘要「上一轮产物」就是上一轮生成的图，对它处理时仍用 from_previous=true，
  不要改用当前画布。
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

_REVIEW_SYSTEM = """你是修图计划复审员。核对用户指令与已排步骤：
- 每一项要求都有对应步骤，且没有用户没要求的步骤 → 不调用任何工具，直接回复：完整
- 还有要求没有对应步骤 → 只为缺失的要求调用工具补步骤（可一次多个），
  不要重复或改写已有步骤，不要添加用户没要求的控制性步骤
  （如隐藏图层、调透明度、调顺序；拆层只允许多颜色主体/提升为图层的前置）
- 复审补充的步骤会与服务端已排步骤做语义去重与结构校验，重复或无依据的补充会被丢弃

用户指令：
{goal}

已排步骤：
{steps}

当前画布：{context}"""

# 复审只允许补「缺失的用户要求」；这些纯控制性工具不构成任何缺失要求（AC-029）
_BLOCKED_ADDITION_TOOLS = {"set_layer_visible", "set_layer_opacity", "reorder_layer"}
# 拆层只允许作为需要独立主体的工具的前置（AC-029）
_NEEDS_SUBJECT_TOOLS = {"create_color_variants", "promote_object_to_layer"}
# 只排前置/控制类步骤的计划没有任何交付物；复审首轮放行时需追问一次（AC-028）
_PREREQUISITE_ONLY_TOOLS = {
    "split_layers",
    "promote_object_to_layer",
    "set_layer_visible",
    "set_layer_opacity",
    "reorder_layer",
    "move_layer",
}
_REVIEW_CLARIFY = "这条指令的计划可能不完整，自动补全没有成功：请拆成两句分别发送，或换一种说法。"
_REVIEW_SECOND_CHANCE = """补充核对：已排步骤只有布局/结构类操作（如拆层、移动、显隐），
执行完不会产生任何新的图像内容。逐项核对用户指令里要求的每一个交付
（例如：多种颜色的独立主体、新生成的图、营销物料、投放尺寸、改色、替换）。
规则：只要用户消息里出现任何图像内容类要求，而已排步骤不会产生它，
就必须为它调用工具补步骤；只有用户消息纯粹只要求布局/结构操作时才回复：完整。
拿不准时选择补步骤——补充会先展示给用户确认，可以取消。"""

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


def _canonical_params(tool: str, params: dict) -> dict:
    """按工具 Schema 归一化：省略值、显式 null、默认值与类型统一到同一形态。

    归一化只用于等价比较（AC-026）；不写回计划——初始步骤保留模型原始参数。
    """
    return tool_service.validate(tool, params)


def _step_key(tool: str, canonical: dict) -> tuple[str, str]:
    return (tool, json.dumps(canonical, sort_keys=True, ensure_ascii=False))


async def _review_call(state: AgentState, *, extra: str | None = None):
    """执行一次复审调用；异常返回 None（调用方决定重试或停止）。"""
    steps_summary = "\n".join(
        f"{index}. {step['tool']} {json.dumps(step.get('params') or {}, ensure_ascii=False)}"
        for index, step in enumerate(state["plan"], start=1)
    )
    prompt = _REVIEW_SYSTEM.format(
        goal=state["goal"], steps=steps_summary, context=state["context"]
    )
    if extra:
        prompt = f"{prompt}\n\n{extra}"
    message = await planner().ainvoke(
        [SystemMessage(prompt), HumanMessage("请复审。")]
    )
    return [
        {"tool": call["name"], "params": call.get("args") or {}}
        for call in (message.tool_calls or [])
    ]


async def _review(state: AgentState) -> AgentState:
    """一次有界复审：双向核对要求覆盖并合并缺失步骤（E2E-F04 / AC-026～029）。

    - 回显的重复步骤按语义归一化去重（省略值/null/默认值视为同一调用），
      但初始计划内部用户明确要求的重复次数不折叠（AC-027）。
    - 纯控制性补充（隐藏/透明度/顺序）与无依赖的拆层补充直接丢弃（AC-029）；
      拆层作为变体/提层前置时插入到依赖步骤之前。
    - 复审补充无效（未知工具/非法参数）或复审调用失败（一次重试后仍失败）时
      安全停止：清空计划并给出可读澄清，绝不静默执行不完整计划冒充完成（AC-028）。
    """
    if not state["plan"]:
        return {}

    async def call_review(extra: str | None = None):
        try:
            return await _review_call(state, extra=extra)
        except Exception:
            try:
                return await _review_call(state, extra=extra)
            except Exception:
                return None

    additions = await call_review()
    if additions is None:
        return {"plan": [], "reply": _REVIEW_CLARIFY}
    if not additions:
        # 计划只有前置/控制类步骤时没有任何交付物：首轮复审放行不可信，
        # 追问一次「执行完会出现用户要的结果吗」（AC-028，有界 ≤2 次复审）
        if all(step["tool"] in _PREREQUISITE_ONLY_TOOLS for step in state["plan"]):
            additions = await call_review(extra=_REVIEW_SECOND_CHANCE)
            if additions is None:
                return {"plan": [], "reply": _REVIEW_CLARIFY}
    if not additions:
        return {}

    initial = list(state["plan"])
    try:
        initial_keys = {
            _step_key(step["tool"], _canonical_params(step["tool"], step.get("params") or {}))
            for step in initial
        }
    except (UnknownTool, tool_service.InvalidParams):
        return {}  # 初始计划本身过不了校验：交给 _verify 统一处理
    kept: list[dict] = []
    for addition in additions:
        try:
            canonical = _canonical_params(addition["tool"], addition["params"])
        except (UnknownTool, tool_service.InvalidParams):
            # 复审认为缺了什么但表达不可用：不能静默按不完整计划执行（AC-028）
            return {"plan": [], "reply": _REVIEW_CLARIFY}
        if _step_key(addition["tool"], canonical) in initial_keys:
            continue  # 语义等价的回显：丢弃，不折叠初始内的有意重复
        merged_tools = {step["tool"] for step in initial} | {step["tool"] for step in kept}
        if addition["tool"] in _BLOCKED_ADDITION_TOOLS:
            continue  # 无依据的控制性步骤：丢弃（AC-029）
        if addition["tool"] == "split_layers" and not (
            merged_tools & _NEEDS_SUBJECT_TOOLS
        ):
            continue  # 没有需要独立主体的步骤：拆层不是前置，丢弃（AC-029）
        kept.append({"tool": addition["tool"], "params": canonical})
    if not kept:
        return {}

    merged = list(initial)
    prerequisite = [step for step in kept if step["tool"] == "split_layers"]
    others = [step for step in kept if step["tool"] != "split_layers"]
    if prerequisite:
        insert_at = next(
            (
                index
                for index, step in enumerate(merged)
                if step["tool"] in _NEEDS_SUBJECT_TOOLS
            ),
            len(merged),
        )
        merged[insert_at:insert_at] = prerequisite
    merged.extend(others)
    try:
        return {"plan": validate(merged)}
    except (UnknownTool, tool_service.InvalidParams, PlanError):
        return {"plan": [], "reply": _REVIEW_CLARIFY}


@lru_cache
def _graph():
    builder = StateGraph(AgentState)
    builder.add_node("plan", _plan)
    builder.add_node("review", _review)
    builder.add_node("verify", _verify)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "review")
    builder.add_edge("review", "verify")
    builder.add_edge("verify", END)
    return builder.compile()


async def run(goal: str, context: str) -> tuple[str, list[dict]]:
    """规划并校验一轮指令，返回答复与尚未下发的计划。"""
    state = await _graph().ainvoke({"goal": goal, "context": context, "plan": [], "reply": ""})
    plan = state["plan"]
    single_approval = len(plan) == 1 and any(
        spec_of(step["tool"]).needs_approval for step in plan
    )
    return (
        spoken(state["reply"], plan, confirm_first=single_approval or len(plan) > 1),
        plan,
    )


def spoken(reply: str, plan: list[dict], *, confirm_first: bool = False) -> str:
    """有计划就按步骤说话；没计划才用兜底或截成一句拒绝。"""
    text = (reply or "").strip()
    if plan:
        if text and text != _FALLBACK_REPLY and not _looks_internal(text):
            return text
        labels = "、".join(label_of(step["tool"], step.get("params")) for step in plan)
        if len(plan) > 1:
            return f"将按以下步骤执行：{labels}。确认后开始。"
        if confirm_first:
            return f"将{labels}，确认后开始。"
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
