"""管理端审计写入服务（WI-20261025-02 实现要求 2，PRD §7/§10）。

- record(...)：与业务写入同一事务，只 add 不 commit——随业务事务一起提交或回滚，
  保证「日志不可用时不先提交修改再悄悄丢记录」；
- record_rejected / record_failed：业务事务已回滚后另起独立事务补记脱敏结果，
  补记失败仅告警日志，不影响主响应语义（WI 风险节）；
- summary 走白名单键，绝不含密码/哈希/令牌/完整提示词；
- request_id 取请求头 X-Request-ID（截断至 64 字符），无则 uuid4。
"""

import logging
import uuid

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionFactory
from app.models import AdminAuditLog

logger = logging.getLogger(__name__)

# 动作枚举（阶段 4/5 扩展素材与清理动作）
USER_CREATE = "user_create"
USER_RENAME = "user_rename"
USER_RESET_PASSWORD = "user_reset_password"
USER_DISABLE = "user_disable"
USER_ENABLE = "user_enable"

RESULT_SUCCESS = "success"
RESULT_REJECTED = "rejected"
RESULT_FAILED = "failed"

TARGET_USER = "user"
# target_type 全集（PRD §7）：user / asset / deletion_operation
TARGET_TYPES = frozenset({TARGET_USER})

# summary 白名单键：仅稳定、可读、非敏感的最小变更摘要
_SUMMARY_KEYS = frozenset({"username", "new_username", "status"})
_MAX_SUMMARY_VALUE = 200
_MAX_REQUEST_ID = 64


def request_id_from(request: Request) -> str:
    """请求头 X-Request-ID 优先（超长截断），否则 uuid4。"""
    raw = (request.headers.get("X-Request-ID") or "").strip()
    return raw[:_MAX_REQUEST_ID] if raw else str(uuid.uuid4())


def _sanitize(summary: dict | None) -> dict | None:
    """白名单键值过滤：非白名单键直接丢弃，值统一字符串并截断（PRD §10）。"""
    if not summary:
        return None
    cleaned = {
        key: str(summary[key])[:_MAX_SUMMARY_VALUE]
        for key in _SUMMARY_KEYS
        if summary.get(key) is not None
    }
    return cleaned or None


def record(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    action: str,
    request_id: str,
    summary: dict | None = None,
    result: str = RESULT_SUCCESS,
) -> None:
    """同事务写入：只 add 不 commit，由业务事务统一提交或回滚。"""
    session.add(
        AdminAuditLog(
            actor_id=actor_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            result=result,
            request_id=request_id,
            summary=_sanitize(summary),
        )
    )


async def _record_detached(
    result: str,
    *,
    actor_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    action: str,
    request_id: str,
    summary: dict | None,
) -> None:
    """另起独立事务补记（业务事务已回滚）。补记失败仅告警，不影响主响应。"""
    try:
        async with SessionFactory() as session:
            session.add(
                AdminAuditLog(
                    actor_id=actor_id,
                    target_type=target_type,
                    target_id=target_id,
                    action=action,
                    result=result,
                    request_id=request_id,
                    summary=_sanitize(summary),
                )
            )
            await session.commit()
    except Exception:
        logger.warning("审计补记失败（action=%s, result=%s）", action, result, exc_info=True)


async def record_rejected(
    *,
    actor_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    action: str,
    request_id: str,
    summary: dict | None = None,
) -> None:
    """预期拒绝（409）：业务零写入后补记 result=rejected（脱敏）。"""
    await _record_detached(
        RESULT_REJECTED,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        action=action,
        request_id=request_id,
        summary=summary,
    )


async def record_failed(
    *,
    actor_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    action: str,
    request_id: str,
    summary: dict | None = None,
) -> None:
    """异常失败：业务事务回滚后补记 result=failed（阶段 3/5 故障路径使用）。"""
    await _record_detached(
        RESULT_FAILED,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        action=action,
        request_id=request_id,
        summary=summary,
    )
