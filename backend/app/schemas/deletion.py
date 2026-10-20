"""创意库删除相关 Schema（PRD §8.1、§8.2）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class BatchDeleteIn(BaseModel):
    """批删请求：原始数组 1～100 项、全部为 UUID，空/超量/非法整批 422。"""

    asset_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class DeletionBlocker(BaseModel):
    code: str
    message: str


class SessionDeletionPreview(BaseModel):
    session_id: uuid.UUID
    delete_asset_count: int = Field(ge=0)
    retained_asset_count: int = Field(ge=0)
    blocked: bool
    blockers: list[DeletionBlocker]
    preview_token: str
    expires_at: datetime


class ReferenceOut(BaseModel):
    """409 asset_in_use 时展示的占用会话与引用类型（只含本人信息）。"""

    session_id: uuid.UUID
    title: str
    types: list[str]


class DeletionErrorDetail(BaseModel):
    """新端点的结构化错误 detail；422 参数错误仍沿用 FastAPI 既有校验结构。"""

    code: str
    message: str
    references: list[ReferenceOut] | None = None


class BatchDeleteOut(BaseModel):
    deleted: list[uuid.UUID]
    skipped: list["BatchSkippedItem"]


class BatchSkippedItem(BaseModel):
    id: uuid.UUID
    code: str
    reason: str


BatchDeleteOut.model_rebuild()
