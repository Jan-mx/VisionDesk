import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TIMESTAMPTZ, UUIDBase, enum_column


class AccountDeletionStatus(enum.StrEnum):
    """清理操作状态机（WI 决策 1）：同一目标最多一个未终结操作。"""

    PROCESSING = "processing"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


# 阶段（WI 决策 1/4）：以 String(32) 存储，常量集中在此供任务与端点共用
PHASE_FROZEN = "frozen"
PHASE_DRAINING = "draining"
PHASE_BUSINESS_ROWS = "business_rows"
PHASE_OBJECTS = "objects"
PHASE_FINALIZE = "finalize"


class AccountDeletionOperation(UUIDBase):
    """账号永久清理操作（PRD §7 AccountDeletionOperation）。

    target/actor 一律不建外键：用户墓碑删除后操作行仍可查询（PRD §8.1）。
    部分唯一索引保证同一目标最多一个 processing/failed 操作。
    """

    __tablename__ = "account_deletion_operations"
    __table_args__ = (
        Index(
            "uq_account_deletion_active",
            "target_user_id",
            unique=True,
            postgresql_where=text("status IN ('processing', 'failed')"),
        ),
    )

    # Python 侧 onupdate：SQL 表达式会在提交后置过期，响应序列化时触发懒加载
    # （pydantic 无 greenlet 上下文即报 MissingGreenlet）
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=lambda: datetime.now(UTC)
    )
    target_user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    actor_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    status: Mapped[AccountDeletionStatus] = mapped_column(
        enum_column(AccountDeletionStatus), default=AccountDeletionStatus.PROCESSING
    )
    phase: Mapped[str] = mapped_column(String(32))
    # 预览时点的计数摘要（可空：仅记录性数据，不参与核对）
    scope_digest: Mapped[dict | None] = mapped_column(JSONB, default=None)
    progress_done: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    progress_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 脱敏失败码与详情（仅稳定 code + 异常类型名，不含对象 key/连接串）
    failure_code: Mapped[str | None] = mapped_column(String(64), default=None)
    failure_detail: Mapped[str | None] = mapped_column(String(500), default=None)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, default=None)
