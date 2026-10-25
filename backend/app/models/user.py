import enum
from datetime import datetime

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TIMESTAMPTZ, UUIDBase, enum_column


class UserRole(enum.StrEnum):
    USER = "user"
    ADMIN = "admin"


class UserStatus(enum.StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    DELETING = "deleting"


class User(UUIDBase):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    # 服务端默认值承接迁移（server_default）：模拟迁移前旧账号直插时也拿到 user/active/1
    role: Mapped[UserRole] = mapped_column(
        enum_column(UserRole), server_default=UserRole.USER.value
    )
    status: Mapped[UserStatus] = mapped_column(
        enum_column(UserStatus), server_default=UserStatus.ACTIVE.value
    )
    # 初次成功登录前为空，不从注册时间推算（PRD §6.2）
    last_login_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, default=None)
    token_version: Mapped[int] = mapped_column(Integer, server_default="1")
