import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app import storage
from app.models import Asset
from app.models.asset import AssetKind, AssetSource

TITLE_MAX_CODEPOINTS = 80


class AssetOut(BaseModel):
    id: uuid.UUID
    kind: AssetKind
    source: AssetSource
    image_format: str
    width: int
    height: int
    size_bytes: int
    has_alpha: bool
    # 手动命名；空值由前端回退元信息展示，既有消费方不受影响
    title: str | None = None
    created_at: datetime
    url: str

    @classmethod
    def of(cls, asset: Asset) -> "AssetOut":
        return cls(
            id=asset.id,
            kind=asset.kind,
            source=asset.source,
            image_format=asset.image_format,
            width=asset.width,
            height=asset.height,
            size_bytes=asset.size_bytes,
            has_alpha=asset.has_alpha,
            title=asset.title,
            created_at=asset.created_at,
            url=storage.signed_url(asset.storage_key),
        )


class AssetPatchIn(BaseModel):
    """素材重命名（LIB-FR-006）：trim 后空白归一 null；超 80 码点 422，不截断。"""

    title: str | None = Field(max_length=TITLE_MAX_CODEPOINTS)

    @field_validator("title")
    @classmethod
    def _blank_becomes_none(cls, value: str | None) -> str | None:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class LibraryGroupOut(BaseModel):
    session_id: uuid.UUID | None
    title: str
    updated_at: datetime
    cover: AssetOut
    assets: list[AssetOut]
