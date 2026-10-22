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
    SNAPSHOT = "snapshot"


class AssetSource(enum.StrEnum):
    UPLOAD = "upload"
    GENERATE = "generate"
    TOOL = "tool"


# 会话内部派生 kind：创意库会话组仅展示最新一张快照（当前作品锚点，与编辑器图片墙一致），
# 其余全部不在创意库展示；leftover 与单删/批删仍拒绝全部下列 kind（PRD §3、§7）
UNMANAGEABLE_KINDS = frozenset(
    {AssetKind.MASK, AssetKind.SUBJECT, AssetKind.BACKGROUND, AssetKind.SNAPSHOT}
)

# 会话删除时仍保留的隐藏派生 kind（拆层产物可能有复用价值）；
# 快照为纯展示锚点，随会话一并清理，故不在保留集
SESSION_RETAINED_KINDS = frozenset({AssetKind.MASK, AssetKind.SUBJECT, AssetKind.BACKGROUND})


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
