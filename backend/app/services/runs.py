import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.models.tool_run import RunStatus, ToolRun


class RunNotFound(Exception):
    pass


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
) -> ToolRun:
    run = ToolRun(
        user_id=user_id, session_id=session_id, tool=tool, params=params, stage="等待开始"
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
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
