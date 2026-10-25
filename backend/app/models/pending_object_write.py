import enum
import uuid

from sqlalchemy import Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import UUIDBase, enum_column


class PendingWriteState(enum.StrEnum):
    """在途写入登记三态（WI 决策 2）：屏障与恢复扫描据此协调。"""

    REGISTERED = "registered"
    COMMITTED = "committed"
    ABORTED = "aborted"


class PendingObjectWrite(UUIDBase):
    """同用户在途对象写入登记（PRD §6.4 写入冻结屏障）。

    不建外键：登记先于资产行存在，用户删除后由恢复扫描回收残留行。
    state=registered 的行由写入方（committed/aborted）或恢复扫描（超时残留）
    终结；删除最终完成前必须无 registered 行。
    """

    __tablename__ = "pending_object_writes"
    __table_args__ = (
        Index("ix_pending_object_writes_user", "user_id"),
        Index("ix_pending_object_writes_state_created", "state", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    storage_key: Mapped[str] = mapped_column(String(255))
    # 发起写入的执行代次（worker 上下文），API 直写为空
    run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), default=None)
    retries: Mapped[int | None] = mapped_column(Integer, default=None)
    state: Mapped[PendingWriteState] = mapped_column(
        enum_column(PendingWriteState), default=PendingWriteState.REGISTERED
    )
