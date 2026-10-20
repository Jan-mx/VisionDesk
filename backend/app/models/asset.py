import enum
import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import UUIDBase, enum_column


class AssetKind(enum.StrEnum):
    ORIGINAL = "original"
    GENERATED = "generated"
    SUBJECT = "subject"
    BACKGROUND = "background"
    MASK = "mask"
    MARKETING = "marketing"
    EXPORT = "export"


class AssetSource(enum.StrEnum):
    UPLOAD = "upload"
    GENERATE = "generate"
    TOOL = "tool"


# 会话内部派生 kind：创意库不展示、不参与本期清理（PRD §3、§7）
UNMANAGEABLE_KINDS = frozenset({AssetKind.MASK, AssetKind.SUBJECT, AssetKind.BACKGROUND})


class Asset(UUIDBase):
    __tablename__ = "assets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[AssetKind] = mapped_column(enum_column(AssetKind))
    source: Mapped[AssetSource] = mapped_column(enum_column(AssetSource))
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    image_format: Mapped[str] = mapped_column(String(8))
    width: Mapped[int]
    height: Mapped[int]
    size_bytes: Mapped[int]
    has_alpha: Mapped[bool]
    # 创意库手动命名；空值由前端回退元信息展示（PRD LIB-FR-006）
    title: Mapped[str | None] = mapped_column(String(80), default=None)
