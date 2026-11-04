import uuid

from sqlalchemy import Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import UUIDBase


class AdminAuditLog(UUIDBase):
    """管理端写动作留痕（PRD §7 AdminAuditLog）。

    一律不建外键：审计行不随目标业务行删除而级联消失，actor/target 仅存 UUID 值。
    summary 只允许白名单键值（services.audit 负责脱敏），绝不含密码/哈希/令牌。
    """

    __tablename__ = "admin_audit_logs"
    __table_args__ = (
        Index("ix_admin_audit_logs_target", "target_type", "target_id"),
        Index("ix_admin_audit_logs_actor", "actor_id"),
        Index("ix_admin_audit_logs_created_at", "created_at"),
    )

    actor_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    # user/asset/deletion_operation（阶段 4/5 扩展）
    target_type: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(32))
    # success/rejected/failed
    result: Mapped[str] = mapped_column(String(16))
    request_id: Mapped[str | None] = mapped_column(String(64), default=None)
    # 白名单变更摘要（JSONB 可空）
    summary: Mapped[dict | None] = mapped_column(JSONB, default=None)
    # 阶段 3 账号清理操作关联
    operation_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), default=None)
