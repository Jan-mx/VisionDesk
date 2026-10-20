import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.models.agent_run import AgentRun
from app.models.tool_run import RunStatus, ToolRun
from app.services import guards

_ACTIVE = (RunStatus.QUEUED, RunStatus.RUNNING)


class RunNotFound(Exception):
    pass


class ActiveTasks(Exception):
    """本人存在 queued/running 任务或待确认计划，删除被保守门禁拒绝（LIB-FR-014）。"""


async def has_active_tasks(session: AsyncSession, user_id: uuid.UUID) -> bool:
    tool = await session.scalar(
        select(ToolRun.id)
        .where(ToolRun.user_id == user_id, ToolRun.status.in_(_ACTIVE))
        .limit(1)
    )
    if tool is not None:
        return True
    agent = await session.scalar(
        select(AgentRun.id)
        .where(AgentRun.user_id == user_id, AgentRun.status.in_(_ACTIVE))
        .limit(1)
    )
    return agent is not None


def snapshot(run: ToolRun) -> dict:
    return {
        "id": str(run.id),
        "tool": run.tool,
        "status": run.status,
        "progress": run.progress,
        "stage": run.stage,
        "error": run.error,
        "result": run.result or {},
    }


async def create(
    session: AsyncSession,
    user_id: uuid.UUID,
    tool: str,
    params: dict,
    session_id: uuid.UUID | None = None,
    *,
    locked: bool = True,
) -> ToolRun:
    """登记一次任务。默认自带用户锁临界区（登记 vs 删除互斥）。

    locked=False 供外层已持锁的调用方（如批量提交链路）合入同一事务；
    同一事务内会重校验 session 存在，避免「删除先提交→登记悬空引用」。
    """
    if locked:
        await guards.user_lock(session, user_id)
    if session_id is not None:
        from app.models import EditSession

        exists = await session.scalar(select(EditSession.id).where(EditSession.id == session_id))
        if exists is None:
            from app.services.sessions import SessionNotFound

            raise SessionNotFound
    run = ToolRun(
        user_id=user_id, session_id=session_id, tool=tool, params=params, stage="等待开始"
    )
    session.add(run)
    if locked:
        await session.commit()
        await session.refresh(run)
    else:
        await session.flush()
    return run


async def get(session: AsyncSession, run_id: uuid.UUID, user_id: uuid.UUID) -> ToolRun:
    run = await session.scalar(
        select(ToolRun).where(ToolRun.id == run_id, ToolRun.user_id == user_id)
    )
    if run is None:
        raise RunNotFound
    if is_stale(run, _stale_seconds()):
        await _expire(session, [run])
    return run


async def load(session: AsyncSession, run_id: uuid.UUID) -> ToolRun:
    run = await session.get(ToolRun, run_id)
    if run is None:
        raise RunNotFound
    return run


async def _commit(session: AsyncSession, run: ToolRun) -> None:
    await session.commit()
    await events.publish(run.id, snapshot(run))


async def start(session: AsyncSession, run: ToolRun) -> None:
    now = datetime.now(UTC)
    run.status = RunStatus.RUNNING
    run.started_at = run.started_at or now
    run.heartbeat_at = now
    run.retries += 1
    run.progress = 5
    run.stage = "已开始"
    await _commit(session, run)


async def report(
    session: AsyncSession,
    run: ToolRun,
    progress: int,
    stage: str,
    result: dict | None = None,
) -> None:
    run.heartbeat_at = datetime.now(UTC)
    run.progress = max(run.progress, progress)
    run.stage = stage
    if result is not None:
        run.result = result
    await _commit(session, run)


async def finish(
    session: AsyncSession,
    run: ToolRun,
    *,
    status: RunStatus,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    """任务只有取得写入资格才能标终态（PRD §7.1）。

    迟到的旧执行（被取消/回收/重试接管）在这里被静默丢弃，
    不得改写当前状态；API 取消与失联收割路径无执行上下文，照常生效。
    """
    await guards.user_lock(session, run.user_id)
    try:
        await guards.assert_writable(session)
    except guards.StaleExecution:
        return
    run.status = status
    run.result = result or {}
    run.error = error
    run.progress = 100 if status is RunStatus.SUCCEEDED else run.progress
    run.stage = "已完成" if status is RunStatus.SUCCEEDED else "已结束"
    run.finished_at = datetime.now(UTC)
    run.heartbeat_at = run.finished_at
    await _commit(session, run)


def is_stale(run: ToolRun, seconds: int, now: datetime | None = None) -> bool:
    if run.status.is_terminal:
        return False
    now = now or datetime.now(UTC)
    reference = run.heartbeat_at or run.started_at or run.created_at
    return reference < now - timedelta(seconds=seconds)


async def expire_stale(session: AsyncSession, seconds: int | None = None) -> int:
    seconds = seconds or _stale_seconds()
    cutoff = datetime.now(UTC) - timedelta(seconds=seconds)
    records = list(
        await session.scalars(
            select(ToolRun).where(
                ToolRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]),
                (
                    (ToolRun.heartbeat_at.is_not(None) & (ToolRun.heartbeat_at < cutoff))
                    | (
                        ToolRun.heartbeat_at.is_(None)
                        & ToolRun.started_at.is_not(None)
                        & (ToolRun.started_at < cutoff)
                    )
                    | (
                        ToolRun.heartbeat_at.is_(None)
                        & ToolRun.started_at.is_(None)
                        & (ToolRun.created_at < cutoff)
                    )
                ),
            )
        )
    )
    await _expire(session, records)
    return len(records)


async def _expire(session: AsyncSession, records: list[ToolRun]) -> None:
    if not records:
        return
    now = datetime.now(UTC)
    for run in records:
        run.status = RunStatus.FAILED
        run.error = "任务执行超时或工作进程已中断，请重试"
        run.stage = "已结束"
        run.finished_at = now
        run.heartbeat_at = now
    await session.commit()
    for run in records:
        await events.publish(run.id, snapshot(run))


def _stale_seconds() -> int:
    from app.config import get_settings

    return get_settings().run_stale_seconds
