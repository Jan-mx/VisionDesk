"""账号删除预览聚合与范围摘要（WI-20261025-03 实现要求 3）。

- 计数一律 COUNT(*)/SUM 全量口径：不受列表页 limit、可见 kind 或隐藏状态影响
  （PRD §6.3.1）；无法识别的对象统计不填 0（列举失败由调用方 503）。
- 范围摘要（scope digest）只含数据库侧事实（会话/历史/素材计数与键清单）：
  确认流程在用户锁内重算比对，S3 调用不得进入数据库事务锁（WI 红线）；
  S3 对象变化由清理任务按前缀实扫兜底，不依赖预览时点的对象清单。
- 任务计数（tool_runs/agent_runs）仅用于展示与活动门禁，不入摘要：
  预览后新增任务须在确认时报 409 active_tasks，而非范围变化。
"""

import hashlib
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, Asset, EditHistory, EditSession, ToolRun, User


class TargetUserNotFound(Exception):
    pass


def user_prefix(user_id: uuid.UUID) -> str:
    """精确对象前缀：末尾保留斜杠，避免相邻 UUID 键形被误包含（PRD §10）。"""
    return f"users/{user_id}/"


async def _count(session: AsyncSession, model, user_id: uuid.UUID) -> int:
    return await session.scalar(
        select(func.count()).select_from(model).where(model.user_id == user_id)
    )


async def scope_digest(session: AsyncSession, user_id: uuid.UUID) -> str:
    """数据库侧范围摘要（确认时在目标用户锁内重算比对，WI 决策 3）。"""
    record = await session.scalar(select(User.id).where(User.id == user_id))
    if record is None:
        raise TargetUserNotFound
    asset_ids = sorted(
        str(value)
        for value in await session.scalars(select(Asset.id).where(Asset.user_id == user_id))
    )
    asset_keys = sorted(
        await session.scalars(select(Asset.storage_key).where(Asset.user_id == user_id))
    )
    sessions = await _count(session, EditSession, user_id)
    history = await _count(session, EditHistory, user_id)
    assets = await _count(session, Asset, user_id)
    total_bytes = await session.scalar(
        select(func.coalesce(func.sum(Asset.size_bytes), 0)).where(Asset.user_id == user_id)
    )
    joined = ";".join(
        [
            f"sessions={sessions}",
            f"history={history}",
            f"assets={assets}",
            f"bytes={total_bytes}",
            f"ids={','.join(asset_ids)}",
            f"keys={','.join(asset_keys)}",
        ]
    )
    return hashlib.sha256(joined.encode()).hexdigest()


async def aggregate_counts(session: AsyncSession, user_id: uuid.UUID) -> dict:
    """预览展示计数（PRD §8.3 AccountDeletionPreview.counts 的 DB 半边）。"""
    record = await session.scalar(select(User.id).where(User.id == user_id))
    if record is None:
        raise TargetUserNotFound
    total_bytes = await session.scalar(
        select(func.coalesce(func.sum(Asset.size_bytes), 0)).where(Asset.user_id == user_id)
    )
    return {
        "sessions": await _count(session, EditSession, user_id),
        "history": await _count(session, EditHistory, user_id),
        "tool_runs": await _count(session, ToolRun, user_id),
        "agent_runs": await _count(session, AgentRun, user_id),
        "assets": await _count(session, Asset, user_id),
        "asset_record_bytes": int(total_bytes),
    }


async def orphan_count(session: AsyncSession, user_id: uuid.UUID, keys: list[str]) -> int:
    """前缀对象中无任何素材行对应的数量（孤儿；与对象统计同口径）。"""
    if not keys:
        return 0
    db_keys = set(await session.scalars(select(Asset.storage_key).where(Asset.user_id == user_id)))
    return len([key for key in keys if key not in db_keys])
