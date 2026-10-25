"""管理端用户维护契约（WI-20261025-02 实现要求 4/7/8，PRD §8）。

AdminUserOut 不暴露 password_hash/token_version；PATCH schema extra=forbid 使
role/token_version/last_login_at 等注入一律 422（ADM-AC-020）。
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.user import UserRole, UserStatus
from app.schemas.auth import Credentials, PasswordStr, UsernameStr, normalize_username
from app.services.paging import PageParams


class AdminUserOut(BaseModel):
    """管理端用户视图：含统计聚合（列表与详情一致，WI 实现要求 7/8）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    role: UserRole
    status: UserStatus
    created_at: datetime
    last_login_at: datetime | None
    asset_count: int
    asset_record_bytes: int
    session_count: int
    tool_run_count: int
    agent_run_count: int


class AdminUserCreate(Credentials):
    """创建普通账号：与注册同一套凭据校验与唯一性（WI 实现要求 4）。"""


class AdminUserPatch(BaseModel):
    """改名与启停：只允许 username / status(active|disabled) 二选一或并列。

    extra="forbid"：role、token_version、last_login_at、deleting 等字段一律 422，
    普通账号不能由此获得管理员身份、状态机不能被绕过（PRD §8.1）。
    """

    model_config = ConfigDict(extra="forbid")

    username: UsernameStr | None = None
    status: Literal["active", "disabled"] | None = None

    @field_validator("username")
    @classmethod
    def _normalize(cls, value: str | None) -> str | None:
        return None if value is None else normalize_username(value)

    @model_validator(mode="after")
    def _requires_a_field(self) -> "AdminUserPatch":
        if self.username is None and self.status is None:
            raise ValueError("至少提供 username 或 status 之一")
        return self


class ResetPasswordInput(BaseModel):
    """重置密码专用输入：与注册同一套密码长度约束（WI 实现要求 4）。"""

    model_config = ConfigDict(extra="forbid")

    password: PasswordStr


class UserListParams(PageParams):
    """GET /admin/users 查询参数。

    sort 白名单 created_at/username/status（Literal）；时间筛选必须带时区
    （AwareDatetime，无时区 422），区间反向 422（WI 实现要求 3/4）。
    """

    sort: Literal["created_at", "username", "status"] = "created_at"
    q: Annotated[str | None, Field(max_length=64)] = None
    status: UserStatus | None = None
    created_from: AwareDatetime | None = None
    created_to: AwareDatetime | None = None

    @model_validator(mode="after")
    def _time_range_order(self) -> "UserListParams":
        if self.created_from and self.created_to and self.created_from > self.created_to:
            raise ValueError("created_from 不得晚于 created_to")
        return self
