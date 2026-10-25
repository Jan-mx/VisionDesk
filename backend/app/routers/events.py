import asyncio
import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.db import SessionDep
from app.deps import CurrentUser
from app.models import User
from app.models.tool_run import RunStatus
from app.models.user import UserStatus
from app.services import runs
from app.services.runs import RunNotFound

router = APIRouter(prefix="/events", tags=["events"])

_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# 超时后主动断开，客户端重连时会先收到快照，不会丢状态
_MAX_STREAM_SECONDS = 600.0


def _frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _session_valid(session: AsyncSession, user_id: uuid.UUID, expected_ver: int) -> bool:
    """逐 tick 轻查询用户状态与会话版本，禁用或版本递增后旧流立即失去续期资格。"""
    row = (
        await session.execute(
            select(User.status, User.token_version).where(User.id == user_id)
        )
    ).first()
    return (
        row is not None
        and row.status == UserStatus.ACTIVE
        and row.token_version == expected_ver
    )


@router.get("/runs/{run_id}")
async def stream_run(run_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Response:
    # 入口鉴权通过时钉住版本基准；流内只复核，不再签发新版本
    expected_ver = user.token_version
    try:
        await runs.get(session, run_id, user.id)
    except RunNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在") from exc

    async def stream() -> AsyncIterator[str]:
        # 先订阅再读快照，否则任务在两步之间结束会让连接一直空等
        async with events.subscribe(run_id) as messages:
            run = await runs.get(session, run_id, user.id)
            yield _frame(runs.snapshot(run))
            if run.status.is_terminal:
                return

            deadline = asyncio.get_running_loop().time() + _MAX_STREAM_SECONDS
            async for payload in messages:
                # 每次唤醒（事件或 ≤15s 空闲 tick）复核会话，失效即 return 断流
                # （PRD §6.2：下一次事件/保活检查时终止，最长 15 秒）
                if not await _session_valid(session, user.id, expected_ver):
                    return
                if payload is None:
                    if asyncio.get_running_loop().time() > deadline:
                        return
                    yield ": ping\n\n"
                    continue
                yield _frame(payload)
                if RunStatus(payload["status"]).is_terminal:
                    return

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_HEADERS)
