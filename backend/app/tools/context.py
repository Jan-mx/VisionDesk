import asyncio
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.edits.render import WHITE, flatten
from app.layers import LayerDocument
from app.models import EditSession, ToolRun
from app.models.asset import Asset, AssetKind, AssetSource
from app.services import assets, sessions

logger = logging.getLogger(__name__)


class ToolError(Exception):
    """可直接展示给用户的工具失败。"""


async def require_session(session: AsyncSession, run: ToolRun) -> EditSession:
    if run.session_id is None:
        raise ToolError("此工具需要在编辑会话中使用")
    try:
        return await sessions.load(session, run.session_id)
    except sessions.SessionNotFound as exc:
        raise ToolError("会话不存在") from exc


def document_of(record: EditSession) -> LayerDocument:
    return LayerDocument.model_validate(record.document)


async def flatten_session(
    session: AsyncSession,
    record: EditSession,
    *,
    background: tuple[int, int, int, int] = WHITE,
    include_text: bool = True,
    document: LayerDocument | None = None,
) -> bytes:
    """把文档拍平为 PNG，再交给像素工具；默认取会话当前文档。"""
    doc = document if document is not None else document_of(record)
    images: dict[uuid.UUID, bytes] = {}
    for layer in doc.layers:
        if layer.asset_id is None:
            continue
        asset = await assets.get_for_user(session, record.user_id, layer.asset_id)
        if asset is None:
            continue
        images[asset.id] = await storage.get(asset.storage_key)
    return await asyncio.to_thread(
        flatten, doc, images, background=background, include_text=include_text
    )


async def snapshot_canvas(
    session: AsyncSession, run: ToolRun, record: EditSession, document: LayerDocument
) -> Asset:
    """拍平画布生成快照素材：多层图层级修改后锚定图片墙「当前作品」。"""
    data = await flatten_session(session, record, document=document)
    return await assets.create_from_bytes(
        session, run.user_id, data, AssetKind.SNAPSHOT, AssetSource.TOOL
    )


def attach_snapshot(result: dict, snapshot: Asset) -> None:
    """把快照挂到工具结果上：入墙并切换「当前作品」。"""
    result["asset_ids"] = [*result.get("asset_ids", []), str(snapshot.id)]
    result["adopt_asset_id"] = str(snapshot.id)
