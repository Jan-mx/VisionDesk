"""AER-FR-001：基于同一源主体的多颜色变体——每色一个独立可编辑图层。"""

import io
import uuid

from PIL import Image
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.layers import Layer, LayerKind, Transform
from app.models.asset import AssetKind, AssetSource
from app.models.tool_run import ToolRun
from app.providers import EditRequest, ProviderError, get_image_provider
from app.services import assets, runs
from app.tools.base import HIDDEN_MASK, LayerRef, MaskRef, ToolSpec
from app.tools.context import ToolError, document_of, require_session
from app.tools.target import layer_image, layer_under_mask, resolve_target, selection_mask

MAX_COLORS = 4
_TRIM_AREA_RATIO = 0.9  # 主体占层面积小于九成时才裁到主体包围盒


class ColorVariantsIn(LayerRef, MaskRef):
    colors: list[str] = Field(
        min_length=1,
        max_length=MAX_COLORS,
        description="要生成的颜色列表（最多 4 种），每种颜色一个独立主体图层",
    )


def _trim_box(image: Image.Image) -> tuple[int, int, int, int] | None:
    """透明层的主体远小于整层时裁到主体包围盒（省 token 且聚焦主体）。"""
    if image.mode != "RGBA":
        return None
    box = image.getchannel("A").getbbox()
    if box is None:
        return None
    width, height = image.size
    if (box[2] - box[0]) * (box[3] - box[1]) > _TRIM_AREA_RATIO * width * height:
        return None
    pad = int(0.05 * max(width, height))
    return (
        max(0, box[0] - pad),
        max(0, box[1] - pad),
        min(width, box[2] + pad),
        min(height, box[3] + pad),
    )


async def create_color_variants_exec(session: AsyncSession, run: ToolRun) -> dict:
    """固定源层与源字节，每个颜色独立请求同一源；禁止链式累计变形。

    每个成功颜色一个新图层与独立素材，画布右侧错位摆放（画布外可见可拖回）；
    原主体不动；一次 apply_edit 整体可撤销。部分失败时如实报告缺失颜色。
    """
    record = await require_session(session, run)
    document = document_of(record)
    colors = list(dict.fromkeys(run.params["colors"]))  # 去重保序
    mask = await selection_mask(session, record, run.params.get("mask_asset_id"))
    if run.params.get("layer_id"):
        source_layer = resolve_target(document, run.params["layer_id"])
    elif mask is not None:
        source_layer = layer_under_mask(document, mask)
    else:
        source_layer = resolve_target(document, None)

    await runs.report(session, run, 10, "读取主体")
    source_bytes = await layer_image(session, record, source_layer)
    source = Image.open(io.BytesIO(source_bytes)).convert("RGBA")
    trim = _trim_box(source)
    if trim is not None:
        region = source.crop(trim)
        buffer = io.BytesIO()
        region.save(buffer, format="PNG")
        request_image = buffer.getvalue()
        size = region.size
    else:
        request_image = source_bytes
        size = source.size

    created: list[tuple[str, object]] = []
    missing: list[str] = []
    total = len(colors)
    for index, color in enumerate(colors):
        await runs.report(session, run, 15 + int(75 * index / max(1, total)), f"生成{color}版本")
        try:
            images = await get_image_provider().edit(
                EditRequest(
                    prompt=(
                        f"把画面主体的颜色改成{color}，保持商品款式、形状、结构与细节完全不变，"
                        "只改变颜色，背景与构图保持原样。"
                    ),
                    image=request_image,
                    width=size[0],
                    height=size[1],
                    count=1,
                ),
            )
        except ProviderError:
            missing.append(color)
            continue
        asset = await assets.create_from_bytes(
            session, run.user_id, images[0], AssetKind.GENERATED, AssetSource.TOOL
        )
        created.append((color, asset))

    if not created:
        raise ToolError("全部颜色生成失败，请重试")

    rebuilt = document.model_copy(deep=True)
    for offset, (color, asset) in enumerate(created):
        rebuilt.layers.append(
            Layer(
                id=f"variant-{uuid.uuid4().hex[:8]}",
                kind=LayerKind.IMAGE,
                name=f"{source_layer.name}·{color}",
                width=asset.width,
                height=asset.height,
                asset_id=asset.id,
                # 默认错位摆放：画布外右侧成列，可见可拖回（明确布局指令由用户拖拽实现）
                transform=Transform(
                    x=document.width + 16 + offset * (asset.width + 16),
                    y=12 + offset * 28,
                ),
            )
        )

    result = {
        "document": rebuilt.model_dump(mode="json"),
        "asset_ids": [str(asset.id) for _color, asset in created],
        "variants": [{"color": color, "asset_id": str(asset.id)} for color, asset in created],
    }
    if missing:
        result["missing_colors"] = missing
        await runs.report(
            session, run, 95, f"{len(created)}/{total} 完成，缺{'、'.join(missing)}"
        )
    else:
        await runs.report(session, run, 95, f"{total} 个颜色版本已生成")
    return result


CREATE_COLOR_VARIANTS = ToolSpec(
    name="create_color_variants",
    label="多颜色主体",
    description=(
        "基于画布上的同一主体生成多种颜色版本，每种颜色一个独立可编辑图层，"
        "原主体保留。用户要求「每种颜色各一个主体/各一只」时用它；"
        "不要用 generate_image 从零生成，也不要把多个颜色合成一张图。"
    ),
    params=ColorVariantsIn,
    handler=create_color_variants_exec,
    needs_approval=True,
    queued=True,
    session_required=True,
    agent_hidden=HIDDEN_MASK,
    detail=lambda params: (
        f"生成{'、'.join(params.get('colors') or [])}色变体" if params.get("colors") else None
    ),
)
