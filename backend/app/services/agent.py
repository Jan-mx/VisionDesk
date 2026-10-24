import logging
import uuid

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import agent
from app.agent import plan as plan_mod
from app.layers import Layer, LayerDocument, LayerKind, LayerMissing, resolve_layer
from app.models import AgentRun, EditSession, ToolRun
from app.models.tool_run import RunStatus
from app.schemas.agent import ChatContextIn
from app.services import assets, runs, selections, sessions, tools
from app.tools import spec_of

logger = logging.getLogger(__name__)


class TurnNotFound(Exception):
    pass


class CannotConfirm(Exception):
    pass


class CannotCancel(Exception):
    pass


class CannotRetry(Exception):
    pass


HISTORY_TURNS = 8
# 墙上可被批量处理的常规素材种类；排除内部遮罩、拆层件与画布快照
_PROCESSABLE_KINDS = {"original", "generated", "marketing", "export"}


async def describe(
    session: AsyncSession, record: EditSession, context: ChatContextIn | None = None
) -> str:
    """给规划模型的画布摘要：当前画布事实、选中图层、有界历史与可处理素材（AER-FR-002/008/009）。"""
    document = LayerDocument.model_validate(record.document)
    names = [_layer_name(layer) for layer in document.layers]
    parts = [
        f"画幅 {document.width}×{document.height}",
        f"图层 {len(document.layers)} 个，自下而上（{'、'.join(names)}）",
        "layer_id 填图层 id 或名字；不填则作用在选区下最上层图像，换背景作用在背景层",
        f"修订号 {record.revision}",
    ]

    current = await assets.get_for_user(session, record.user_id, record.current_asset_id)
    if current is not None:
        parts.append(f"当前图 {current.image_format}{'，含透明通道' if current.has_alpha else ''}")

    if context is not None and context.selected_layer_id:
        try:
            selected = resolve_layer(document, context.selected_layer_id)
        except LayerMissing:
            selected = None  # 客户端选择过期：不进入摘要，规划按默认目标
        if selected is not None:
            parts.append(f"当前选中图层：{selected.id}（{_layer_name(selected)}）")

    history = await _recent_history(session, record)
    if history:
        parts.append(f"最近对话（旧→新）：{'；'.join(history)}")

    wall = await processable_wall_assets(session, record)
    if wall:
        kinds: dict[str, int] = {}
        for asset in wall:
            kinds[asset.kind.value] = kinds.get(asset.kind.value, 0) + 1
        summary = "、".join(f"{kind}{count} 张" for kind, count in kinds.items())
        parts.append(
            f"图片墙可处理素材 {len(wall)} 张（{summary}）；"
            '对这些图批量处理时用 batch_process 并填 assets="wall"，不要猜素材 ID'
        )

    selected_mask = await selections.get(record.id, record.revision)
    if selected_mask:
        markers = selected_mask.get("markers") or []
        where = f"已有选区，{len(markers)} 个标点" if markers else "已有笔刷选区"
        parts.append(f"{where}（选区只限定区域，不限定图层；与 layer_id 同时给出时取交集）")
    else:
        parts.append("当前无选区，区域工具会作用在整个图层")
    return "；".join(parts)


async def _recent_history(session: AsyncSession, record: EditSession) -> list[str]:
    """最近 ≤8 轮已落库对话的有界摘要：目标、结果引用与终态，不含全部正文。"""
    result = await session.execute(
        select(AgentRun.goal, AgentRun.status)
        .where(AgentRun.session_id == record.id)
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(HISTORY_TURNS)
    )
    lines = []
    for goal, status in reversed(result.all()):
        outcome = {
            RunStatus.SUCCEEDED: "已完成",
            RunStatus.FAILED: "失败",
            RunStatus.CANCELED: "已取消",
        }.get(status, "进行中")
        lines.append(f"用户「{goal[:60]}」→{outcome}")
    return lines


async def processable_wall_assets(session: AsyncSession, record: EditSession) -> list:
    """会话当前可见、可交给批量处理的常规素材：排除内部遮罩、拆层件、快照与隐藏素材，按墙序去重。"""
    from app.models.asset import AssetKind

    wall = await sessions.assets_of(session, record)
    seen: set = set()
    result = []
    for asset in wall:
        if asset.library_hidden or asset.kind not in (
            AssetKind.ORIGINAL,
            AssetKind.GENERATED,
            AssetKind.MARKETING,
            AssetKind.EXPORT,
        ):
            continue
        if asset.id in seen:
            continue
        seen.add(asset.id)
        result.append(asset)
    return result


def _layer_name(layer: Layer) -> str:
    text = f"文字「{layer.text[:8]}」" if layer.kind is LayerKind.TEXT and layer.text else None
    name = f"{layer.id}={text or layer.name}"
    return name if layer.visible else f"{name}·隐藏"


async def respond(
    session: AsyncSession,
    record: EditSession,
    goal: str,
    context: ChatContextIn | None = None,
) -> AgentRun:
    """规划一轮指令并落库。多步计划先停住等确认，单步直接开跑。"""
    revision = record.revision
    reply, steps, error = "", [], None

    try:
        reply, steps = await agent.run(goal, await describe(session, record, context))
        steps = await _pin_selection(session, record, steps)
        steps, pin_error = await _pin_batch_targets(session, record, steps, context)
        if pin_error:
            reply, steps = pin_error, []
    except agent.PlannerUnavailable as exc:
        error = str(exc)
    except Exception:
        logger.exception("指令规划异常 session_id=%s", record.id)
        await session.rollback()
        error = "规划失败，请重试"

    if error:
        status = RunStatus.FAILED
    elif plan_mod.needs_confirm(steps):
        status = RunStatus.QUEUED
    elif steps:
        status = RunStatus.RUNNING
    else:
        status = RunStatus.SUCCEEDED

    turn = AgentRun(
        user_id=record.user_id,
        session_id=record.id,
        revision=revision,
        goal=goal,
        reply=reply,
        plan=steps,
        status=status,
        error=error,
    )
    # 规划（模型调用）已结束才取锁登记，与删除形成串行临界区（PRD §7.1）
    from app.services import guards

    await guards.user_lock(session, record.user_id)
    session.add(turn)
    await session.commit()
    await session.refresh(turn)

    if turn.status is RunStatus.RUNNING:
        await _advance(session, turn)
    return turn


async def _pin_selection(
    session: AsyncSession, record: EditSession, steps: list[dict]
) -> list[dict]:
    """把当轮选区钉进每个需要遮罩的步骤。

    清除选区与修订号递增都发生在第一步之后，显式 id 才能让后续步骤共用同一块区域。
    """
    stored = await selections.get(record.id, record.revision)
    mask_asset_id = (stored or {}).get("mask_asset_id")
    if not mask_asset_id:
        return steps
    for step in steps:
        params = step["params"]
        if _wants_mask(step["tool"]) and not params.get("mask_asset_id"):
            step["params"] = {**params, "mask_asset_id": mask_asset_id}
    return steps


def _wants_mask(tool: str) -> bool:
    return "mask_asset_id" in spec_of(tool).params.model_fields


async def _pin_batch_targets(
    session: AsyncSession,
    record: EditSession,
    steps: list[dict],
    context: ChatContextIn | None,
) -> tuple[list[dict], str | None]:
    """把聊天批量步骤的素材目标钉成真实 asset_ids（AER-FR-009）。

    显式选择集合优先；否则 assets="wall" 解析为会话可见可处理常规素材。
    目标为空、越权或超上限时返回可读错误并放弃本轮计划，不静默截断。
    """
    from app.schemas.batch import MAX_BATCH_ASSETS

    error: str | None = None
    for step in steps:
        if step["tool"] != "batch_process" or step["params"].get("asset_ids"):
            continue
        explicit = list(context.selected_asset_ids) if context else []
        if explicit:
            resolved, error = await _validate_batch_targets(session, record, explicit)
        elif step["params"].get("assets") == "wall":
            wall = await processable_wall_assets(session, record)
            resolved = [asset.id for asset in wall]
            if not resolved and error is None:
                error = "图片墙上没有可批量处理的素材"
            elif len(resolved) > MAX_BATCH_ASSETS and error is None:
                error = (
                    f"选中了 {len(resolved)} 张图，超过批量上限 {MAX_BATCH_ASSETS} 张，"
                    "请缩小范围后再试"
                )
        else:
            resolved = []
            error = "批量处理需要先说明处理哪些图（在图片墙勾选，或说清范围）"
        if error:
            return [], error
        step["params"] = {
            **step["params"],
            "asset_ids": [str(asset_id) for asset_id in resolved],
        }
    return steps, None


async def _validate_batch_targets(
    session: AsyncSession, record: EditSession, asset_ids: list
) -> tuple[list, str | None]:
    """显式集合逐个校验归属与可处理性（AER-AC-018：拒绝越权、隐藏与内部素材）。"""
    from app.models.asset import AssetKind
    from app.schemas.batch import MAX_BATCH_ASSETS

    allowed = {kind for kind in AssetKind if kind.value in _PROCESSABLE_KINDS}
    if len(asset_ids) > MAX_BATCH_ASSETS:
        return [], f"一次最多批量处理 {MAX_BATCH_ASSETS} 张图，请缩小选择范围"
    resolved = []
    for asset_id in asset_ids:
        asset = await assets.get_for_user(session, record.user_id, asset_id)
        if asset is None:
            return [], "选择中有素材不存在或无权访问，请刷新图片墙后重试"
        if asset.library_hidden or asset.kind not in allowed:
            return [], "选择中有素材不可处理（已隐藏或内部素材），请重新选择"
        resolved.append(asset_id)
    return resolved, None


async def confirm(session: AsyncSession, record: EditSession, turn_id: uuid.UUID) -> AgentRun:
    turn = await _get(session, record, turn_id)
    steps = _copy(turn.plan)
    waiting = [step for step in steps if step["status"] == plan_mod.WAITING]
    if turn.status is not RunStatus.QUEUED and not waiting:
        raise CannotConfirm
    for step in waiting:
        step["status"] = plan_mod.PENDING
        step["approved"] = True
    turn.plan = steps
    turn.status = RunStatus.RUNNING
    _touch(turn)
    await session.commit()
    return await _advance(session, turn)


async def cancel(session: AsyncSession, record: EditSession, turn_id: uuid.UUID) -> AgentRun:
    turn = await _get(session, record, turn_id)
    if turn.status.is_terminal:
        raise CannotCancel
    turn.plan = plan_mod.cancel_remaining(_copy(turn.plan))
    await _cancel_queued_runs(session, turn.plan)
    _touch(turn)
    turn.status = RunStatus(plan_mod.settle(turn.plan))
    await session.commit()
    await session.refresh(turn)
    return turn


async def retry(session: AsyncSession, record: EditSession, turn_id: uuid.UUID) -> AgentRun:
    turn = await _get(session, record, turn_id)
    steps = _copy(turn.plan)
    if not any(step["status"] in {plan_mod.FAILED, plan_mod.BLOCKED} for step in steps):
        raise CannotRetry
    plan_mod.retryable(steps)
    turn.plan = steps
    turn.status = RunStatus.RUNNING
    turn.error = None
    _touch(turn)
    await session.commit()
    return await _advance(session, turn)


async def continue_plan(session: AsyncSession, run: ToolRun) -> None:
    """某一步结束后推进后续依赖，刷新后也能从已落库的计划接着跑。"""
    if run.session_id is None:
        return
    turn = await _turn_of_run(session, run)
    if turn is None:
        return

    steps = _copy(turn.plan)
    for step in steps:
        if step.get("run_id") == str(run.id):
            step["status"] = run.status.value
    turn.plan = steps
    _touch(turn)
    # 先不落库：同步的下一步（如翻转）跟这次一起提交，前端不会捞到「上一步完了、下一步还没开始」
    await _advance(session, turn)


async def turns_of(
    session: AsyncSession,
    record: EditSession,
    *,
    limit: int = 50,
    before: uuid.UUID | None = None,
) -> list[AgentRun]:
    """默认返回最近 limit 轮（时间正序展示）；带 before 游标时返回更早的一页。

    (created_at, id) 双键排序保证同时间消息的稳定顺序，翻页无重复遗漏。
    """
    query = select(AgentRun).where(AgentRun.session_id == record.id)
    if before is not None:
        anchor = await session.scalar(
            select(AgentRun).where(AgentRun.id == before, AgentRun.session_id == record.id)
        )
        if anchor is None:
            return []
        query = query.where(tuple_(AgentRun.created_at, AgentRun.id) < (anchor.created_at, anchor.id))
    result = await session.scalars(
        query.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit)
    )
    return list(reversed(list(result)))


class PlanInputError(Exception):
    """步骤产物引用无法解析（上游缺失/多产物未指定），可直接展示给用户。"""


async def _resolve_step_input(
    session: AsyncSession, steps: list[dict], step: dict
) -> dict:
    """解析 from_previous 步骤的输入来源：最近的已成功前置步骤的指定产物（AER-FR-003）。

    引用随 plan 持久化（解析结果写回 params），刷新与 Worker 重启后不依赖内存；
    单步重试时上游已成功不重跑，直接复用其产物。
    """
    params = step["params"]
    if not params.get("from_previous"):
        return params
    by_id = {other["id"]: other for other in steps}
    upstream = next(
        (
            by_id[dep]
            for dep in reversed(step.get("depends_on") or [])
            if by_id[dep]["status"] == plan_mod.SUCCEEDED and by_id[dep].get("run_id")
        ),
        None,
    )
    if upstream is None:
        raise PlanInputError("要衔接的前置步骤还没有成功结果，无法引用它的产物")
    run = await session.get(ToolRun, uuid.UUID(upstream["run_id"]))
    produced = ((run.result if run else None) or {}).get("asset_ids") or []
    if not produced:
        raise PlanInputError("前置步骤没有可用的图片产物")
    if len(produced) > 1:
        index = params.get("previous_index")
        if index is None:
            raise PlanInputError(
                f"前置步骤产生了 {len(produced)} 张图片，请说明用第几张（previous_index）"
            )
        if not isinstance(index, int) or not 1 <= index <= len(produced):
            raise PlanInputError(f"previous_index 超出范围（1 到 {len(produced)}）")
        chosen = produced[index - 1]
    else:
        chosen = produced[0]
    return {**params, "source_asset_id": chosen}


async def _advance(session: AsyncSession, turn: AgentRun) -> AgentRun:
    steps = _copy(turn.plan)
    progressed = True
    while progressed:
        progressed = False
        for step in plan_mod.ready(steps):
            if spec_of(step["tool"]).needs_approval and not step.get("approved"):
                step["status"] = plan_mod.WAITING
                continue
            try:
                params = await _resolve_step_input(session, steps, step)
            except PlanInputError as exc:
                # 产物引用解析失败：步骤失败并带可读原因，绝不回落画布（AER-FR-003）
                step["status"] = plan_mod.FAILED
                turn.error = str(exc)
                progressed = False
                break
            try:
                run = await tools.submit(
                    session, turn.user_id, step["tool"], params, turn.session_id
                )
            except sessions.SessionNotFound:
                # 会话在推进过程中被删除：后续步骤不再登记，整轮标记失败收尾
                step["status"] = plan_mod.FAILED
                turn.error = "会话已删除，计划已终止"
                progressed = False
                break
            step["run_id"] = str(run.id)
            step["status"] = run.status.value
            if params is not step["params"]:
                step["params"] = params
            turn.plan = steps
            turn.status = RunStatus.RUNNING
            _touch(turn)
            await session.commit()
            progressed = run.status is RunStatus.SUCCEEDED

    turn.plan = steps
    plan_mod.mark_blocked(steps)
    turn.status = RunStatus(plan_mod.settle(steps))
    if turn.status is RunStatus.FAILED and not turn.error:
        turn.error = await _first_failure(session, steps)
    waiting = any(step["status"] == plan_mod.WAITING for step in steps)
    if turn.status is RunStatus.QUEUED and waiting:
        turn.reply = turn.reply or "下一步需要确认后再执行。"
    _touch(turn)
    await session.commit()
    await session.refresh(turn)
    return turn


async def _first_failure(session: AsyncSession, steps: list[dict]) -> str:
    """首个失败步骤的可读原因，给整轮失败一个界面可见的解释。"""
    for step in steps:
        if step["status"] != plan_mod.FAILED or not step.get("run_id"):
            continue
        run = await session.get(ToolRun, uuid.UUID(step["run_id"]))
        if run is not None and run.error:
            return run.error
    return "有步骤失败，可重试或取消"


async def _cancel_queued_runs(session: AsyncSession, steps: list[dict]) -> None:
    for step in steps:
        if not step.get("run_id") or step["status"] != plan_mod.QUEUED:
            continue
        run = await session.get(ToolRun, uuid.UUID(step["run_id"]))
        if run is None or run.status is not RunStatus.QUEUED:
            continue
        await runs.finish(session, run, status=RunStatus.CANCELED, error="已取消")
        step["status"] = plan_mod.CANCELED


async def _get(session: AsyncSession, record: EditSession, turn_id: uuid.UUID) -> AgentRun:
    turn = await session.scalar(
        select(AgentRun).where(AgentRun.id == turn_id, AgentRun.session_id == record.id)
    )
    if turn is None:
        raise TurnNotFound
    return turn


async def _turn_of_run(session: AsyncSession, run: ToolRun) -> AgentRun | None:
    turns = await session.scalars(
        select(AgentRun)
        .where(AgentRun.session_id == run.session_id)
        .order_by(AgentRun.created_at.desc())
        .limit(20)
    )
    run_id = str(run.id)
    for turn in turns:
        if any(step.get("run_id") == run_id for step in turn.plan):
            return turn
    return None


def _copy(steps: list) -> list[dict]:
    return [dict(step) for step in steps]


def _touch(turn: AgentRun) -> None:
    flag_modified(turn, "plan")
