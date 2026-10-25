import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.user import UserRole


class Credentials(BaseModel):
    """注册与登录共用凭据 schema。

    extra="forbid"：注入 role/token_version/status 等管理字段一律 422，
    防止脚手架表单把越权字段送进后端（PRD §8.1 / WI-20261025-01 决策 5）。
    """

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=64)

    @field_validator("username")
    @classmethod
    def _normalize(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.replace("_", "").isalnum():
            raise ValueError("用户名只能包含字母、数字和下划线")
        return normalized


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    role: UserRole

    model_config = {"from_attributes": True}
