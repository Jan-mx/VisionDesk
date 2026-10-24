import io

from PIL import Image, ImageFilter
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.edits.geometry import to_layer_mask
from app.edits.split import EmptyCut, alpha_mask
from app.layers import Layer, LayerDocument
from app.models.asset import AssetKind
from app.models.tool_run import ToolRun
from app.providers import EditRequest, get_image_provider
from app.services import runs, selections
from app.tools.base import HIDDEN_MASK, LayerRef, MaskRef, ToolSpec
from app.tools.context import ToolError, document_of, require_session
from app.tools.target import (
    layer_image,
    layer_under_mask,
    resolve_target,
    selection_mask,
    write_layer_image,
)


class RegionIn(LayerRef, MaskRef):
    prompt: str = Field(default="", max_length=500)


class ReplaceRegionIn(LayerRef, MaskRef):
    prompt: str = Field(min_length=1, max_length=500)


# ROI 边距：给模型目标物周边的上下文，同时把改动范围约束在选区附近
_ROI_PAD_RATIO = 0.08


def _target(document: LayerDocument, layer_id: str | None, mask: bytes | None) -> Layer:
    """点名了图层就改那一层；否则取选区下最上层，没选区就取最上层图像。"""
    if not layer_id and mask is not None:
        return layer_under_mask(document, mask)
    return resolve_target(document, layer_id)


def _layer_luma_mask(
    source: bytes, mask: bytes | None, layer: Layer, canvas: tuple[int, int]
) -> Image.Image | None:
    """无选区时用该层透明通道当区域（返回 None 表示改整层）；有选区取与该层的交集。"""
    if mask is None:
        return None
    try:
        local = to_layer_mask(mask, layer, canvas)
    except EmptyCut as exc:
        raise ToolError("选区没有覆盖到该图层") from exc
    if local.getbbox() is None:
        raise ToolError("选区没有覆盖到该图层")
    return local


def _roi_box(luma: Image.Image, size: tuple[int, int]) -> tuple[int, int, int, int]:
    """选区包围盒加比例边距并裁到层界内。"""
    left, top, right, bottom = luma.getbbox()
    pad = max(4, int(_ROI_PAD_RATIO * max(right - left, bottom - top)))
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(size[0], right + pad)
    bottom = min(size[1], bottom + pad)
    return (left, top, right, bottom)


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def _edit_roi(
    session: AsyncSession,
    run: ToolRun,
    source: Image.Image,
    luma: Image.Image,
    scoped: str,
) -> Image.Image:
    """把选区裁成 ROI 发给模型：输入本身就带目标位置，不靠文字猜（AER-FR-004）。

    只在 ROI 内做遮罩合成再原位贴回，区域外像素按构造保持原样。
    """
    box = _roi_box(luma, source.size)
    roi = source.crop(box)
    roi_mask = luma.crop(box)
    edited = (
        await get_image_provider().edit(
            EditRequest(
                prompt=(
                    f"{scoped} 这张图是原图的目标区域裁剪，只改其中选中的物体，"
                    "区域边缘以外的部分原样保留。"
                ),
                image=_png(roi),
                width=roi.width,
                height=roi.height,
            ),
            on_progress=lambda progress, stage: runs.report(session, run, progress, stage),
        )
    )[0]
    result = Image.open(io.BytesIO(edited)).convert("RGBA")
    if result.size != roi.size:
        result = result.resize(roi.size, Image.Resampling.LANCZOS)
    merged = Image.composite(result, roi, roi_mask.filter(ImageFilter.GaussianBlur(1.2)))
    output = source.copy()
    output.paste(merged, (box[0], box[1]))
    return output


async def _edit_region(session: AsyncSession, run: ToolRun, *, scoped: str, whole: str) -> dict:
    """按「图层 × 选区」改一层：有选区发 ROI 裁图，没选区改整层。"""
    record = await require_session(session, run)
    if run.params.get("revision") not in (None, record.revision):
        raise ToolError("选区已过期，请重新选择")

    document = document_of(record)
    mask = await selection_mask(session, record, run.params.get("mask_asset_id"))
    layer = _target(document, run.params.get("layer_id"), mask)

    await runs.report(session, run, 20, "读取图层")
    source_bytes = await layer_image(session, record, layer)
    source = Image.open(io.BytesIO(source_bytes)).convert("RGBA")
    luma = _layer_luma_mask(source_bytes, mask, layer, (document.width, document.height))

    await runs.report(session, run, 40, "局部生成")
    if luma is not None:
        output = await _edit_roi(session, run, source, luma, scoped)
    else:
        edited = (
            await get_image_provider().edit(
                EditRequest(
                    prompt=whole,
                    image=source_bytes,
                    width=source.width,
                    height=source.height,
                ),
                on_progress=lambda progress, stage: runs.report(session, run, progress, stage),
            )
        )[0]
        await runs.report(session, run, 85, "合并结果")
        from app.edits.mask import apply_masked

        output = Image.open(
            io.BytesIO(apply_masked(source_bytes, edited, alpha_mask(source_bytes)))
        ).convert("RGBA")

    await runs.report(session, run, 90, "写回图层")
    await selections.clear(record.id)
    return await write_layer_image(
        session, run, record, layer.id, _png(output), AssetKind.GENERATED
    )


async def erase_region_exec(session: AsyncSession, run: ToolRun) -> dict:
    asked = run.params.get("prompt")
    return await _edit_region(
        session,
        run,
        scoped=asked or "移除选中物体，用周围背景自然填补，不要改变选区以外的画面。",
        whole=asked or "移除画面中的物体，用周围背景自然填补。",
    )


async def replace_region_exec(session: AsyncSession, run: ToolRun) -> dict:
    what = run.params["prompt"]
    return await _edit_region(
        session,
        run,
        scoped=f"只改选中区域：{what}。选区外的主体、光线和背景必须保持原样。",
        whole=f"{what}。保持构图、透视与画幅不变，透明区域仍然透明。",
    )


ERASE_REGION = ToolSpec(
    name="erase_region",
    label="局部消除",
    description=(
        "消除物体，并用周围内容自然填补。"
        "有选区时只消除选区内；用 layer_id 点名图层时消除该层内容；两者都有取交集。"
    ),
    params=RegionIn,
    handler=erase_region_exec,
    queued=True,
    session_required=True,
    agent_hidden=HIDDEN_MASK,
)

REPLACE_REGION = ToolSpec(
    name="replace_region",
    label="局部替换",
    description=(
        "按文字描述替换画面内容，改颜色、换材质、换成另一个物体都用它。"
        "有选区时只改选区内；用 layer_id 点名图层时改该层整层；两者都有取交集。"
        "只需给出替换描述，不必确认选区是否点准。"
    ),
    params=ReplaceRegionIn,
    handler=replace_region_exec,
    queued=True,
    session_required=True,
    agent_hidden=HIDDEN_MASK,
)
