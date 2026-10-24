from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.edits.geometry import footprint_corners, overlaps_rect, to_layer_mask
from app.edits.mask import overlay_png, to_luma
from app.edits.pixels import resize_to
from app.edits.split import EmptyCut
from app.layers import (
    BACKGROUND_LAYER_ID,
    BASE_LAYER_ID,
    Layer,
    LayerDocument,
    LayerKind,
    LayerMissing,
    resolve_layer,
)
from app.models import EditSession, ToolRun
from app.models.asset import AssetKind, AssetSource
from app.services import assets, selections
from app.services.selections import EmptySelection, StaleSelection
from app.tools.context import (
    ToolError,
    attach_snapshot,
    document_of,
    logger,
    snapshot_canvas,
)

STALE_SELECTION = "画幅已变，选区失效，请重新选择"


async def selection_mask(
    session: AsyncSession, record: EditSession, mask_asset_id: str | None
) -> bytes | None:
    """取当前选区。返回 None 表示没有选区，由调用方决定是否改整层。"""
    try:
        return await selections.mask_bytes(session, record, mask_asset_id)
    except EmptySelection:
        return None
    except StaleSelection as exc:
        raise ToolError(STALE_SELECTION) from exc


async def require_selection(
    session: AsyncSession, record: EditSession, mask_asset_id: str | None, missing: str
) -> bytes:
    mask = await selection_mask(session, record, mask_asset_id)
    if mask is None:
        raise ToolError(missing)
    return mask


def resolve_target(document: LayerDocument, layer_id: str | None) -> Layer:
    """按 id 或名字取目标图层，点名不到时报可直接展示的错误。"""
    try:
        return resolve_layer(document, layer_id)
    except LayerMissing as exc:
        raise ToolError(str(exc)) from exc


async def layer_image(session: AsyncSession, record: EditSession, layer: Layer) -> bytes:
    if layer.kind is not LayerKind.IMAGE or layer.asset_id is None:
        raise ToolError("该图层没有可处理的图像")
    asset = await assets.get_for_user(session, record.user_id, layer.asset_id)
    if asset is None:
        raise ToolError("图层素材不存在")
    return await storage.get(asset.storage_key)


async def write_layer_image(
    session: AsyncSession,
    run: ToolRun,
    record: EditSession,
    layer_id: str | None,
    data: bytes,
    kind: AssetKind,
) -> dict:
    """把处理结果写回指定图层；单层时直接采用层图，多层时拍平快照锚定「当前作品」。"""
    document = document_of(record).model_copy(deep=True)
    layer = resolve_layer(document, layer_id)
    if layer.kind is not LayerKind.IMAGE:
        raise ToolError("只能对图像图层做此操作")
    asset = await assets.create_from_bytes(session, run.user_id, data, kind, AssetSource.TOOL)
    layer.asset_id = asset.id
    result: dict = {
        "document": document.model_dump(mode="json"),
        "asset_ids": [str(asset.id)],
    }
    if _single_image(document):
        result["adopt_asset_id"] = str(asset.id)
        return result
    try:
        snapshot = await snapshot_canvas(session, run, record, document)
    except Exception:
        # 快照是展示增强：失败降级为只写层，不影响编辑本身
        logger.warning("canvas snapshot failed after writing layer %s", layer_id, exc_info=True)
        return result
    attach_snapshot(result, snapshot)
    return result


async def write_flat_canvas(
    session: AsyncSession,
    run: ToolRun,
    record: EditSession,
    data: bytes,
    kind: AssetKind,
) -> dict:
    """整画布编辑结果的写回：重建单层恒等变换文档（AER-FR-006）。

    输入是已拍平的画布图（旧变换只应用了这一次）；写回原层会保留旧 transform，
    下次渲染再变换一次导致翻转/缩放重复。重建为画幅同尺寸的底图层，
    文字层随拍平烘焙成像素，undo 可整体回滚。
    """
    document = document_of(record)
    source = next(
        (layer for layer in reversed(document.layers) if layer.kind is LayerKind.IMAGE),
        None,
    )
    fitted = resize_to(data, document.width, document.height)
    asset = await assets.create_from_bytes(
        session, run.user_id, fitted, kind, AssetSource.TOOL
    )
    rebuilt = LayerDocument(
        width=document.width,
        height=document.height,
        layers=[
            Layer(
                id=(source.id if source else BASE_LAYER_ID),
                kind=LayerKind.IMAGE,
                name=(source.name if source else "底图"),
                width=document.width,
                height=document.height,
                asset_id=asset.id,
                locked=(source.locked if source else True),
            )
        ],
    )
    return {
        "document": rebuilt.model_dump(mode="json"),
        "asset_ids": [str(asset.id)],
        "adopt_asset_id": str(asset.id),
    }


def background_target(document: LayerDocument) -> Layer:
    for layer in document.layers:
        if layer.id == BACKGROUND_LAYER_ID and layer.kind is LayerKind.IMAGE:
            return layer
    return resolve_layer(document, None)


def layer_under_mask(document: LayerDocument, mask: bytes) -> Layer:
    """选区覆盖到的最上层图像；命中测试用与渲染一致的变换几何（AER-FR-005）。

    矩形相交只看未变换包围盒会把旋转伸展出的对角漏掉、也会在角落空隙误命中，
    这里对变换后的层四边形与选区包围盒做精确相交。
    """
    luma = to_luma(mask, (document.width, document.height))
    box = luma.getbbox()
    if box is None:
        return resolve_layer(document, None)
    rect = (box[0], box[1], box[2] - 1, box[3] - 1)
    for layer in reversed(document.layers):
        if layer.kind is not LayerKind.IMAGE or not layer.visible:
            continue
        if overlaps_rect(footprint_corners(layer), rect):
            return layer
    return resolve_layer(document, None)


def mask_for_layer(mask: bytes, layer: Layer, canvas: tuple[int, int]) -> bytes:
    """画布选区遮罩逆变换到层本地坐标（缩放/旋转/翻转一致，AER-FR-005）。"""
    local = to_layer_mask(mask, layer, canvas)
    if local.getbbox() is None:
        raise EmptyCut
    return overlay_png(local)


def _single_image(document: LayerDocument) -> bool:
    return sum(1 for layer in document.layers if layer.kind is LayerKind.IMAGE) <= 1
