import enum
import uuid

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.edits.pixels import letterbox, resize_to
from app.models.asset import AssetKind, AssetSource
from app.models.tool_run import ToolRun
from app.providers import EditRequest, get_image_provider
from app.ratios import DELIVERY_RATIOS, Ratio, size_of
from app.services import assets, runs
from app.tools.base import ToolSpec
from app.tools.context import ToolError, flatten_session, require_session


class MarketingKind(enum.StrEnum):
    PRODUCT = "product"
    SCENE = "scene"
    MODEL = "model"
    POSTER = "poster"


class PlatformStyle(enum.StrEnum):
    XIAOHONGSHU = "xiaohongshu"
    DOUYIN = "douyin"
    TMALL = "tmall"


_PROMPTS = {
    MarketingKind.PRODUCT: (
        "把商品放在纯白背景上，居中构图，柔和顶光，边缘干净，"
        "保留商品原有材质与颜色，输出电商主图。"
    ),
    MarketingKind.SCENE: (
        "把商品放到真实生活场景中，侧逆光、浅景深，氛围自然，"
        "保留商品原有材质与颜色。"
    ),
    MarketingKind.MODEL: (
        "生成模特手持或佩戴该商品的半身展示图，简洁室内背景、自然光，"
        "商品细节清晰可辨。"
    ),
    MarketingKind.POSTER: (
        "做一张促销海报，突出商品，右侧或下方留出文字区，构图干净有冲击力。"
    ),
}


_PLATFORM_PROMPTS = {
    PlatformStyle.XIAOHONGSHU: (
        "整体呈现小红书风格：明亮通透，清新精致的生活分享调性，"
        "柔和自然光，浅色背景适当留白，保留商品原有材质与颜色。"
    ),
    PlatformStyle.DOUYIN: (
        "整体呈现抖音风格：高饱和高对比，视觉冲击力强，潮流感构图，"
        "保留商品原有材质与颜色。"
    ),
    PlatformStyle.TMALL: (
        "整体呈现天猫京东大促风格：电商促销氛围，预留价格与卖点文字区，"
        "带有节日装饰感，保留商品原有材质与颜色。"
    ),
}

# 平台风格联动出图比例：小红书 4:5、抖音 9:16、天猫京东 1:1
_PLATFORM_RATIOS = {
    PlatformStyle.XIAOHONGSHU: Ratio.PORTRAIT_4_5,
    PlatformStyle.DOUYIN: Ratio.VERTICAL_9_16,
    PlatformStyle.TMALL: Ratio.SQUARE,
}


class GenerateMarketingIn(BaseModel):
    kind: MarketingKind = Field(
        description="product 商品主图、scene 场景氛围图、model 模特上身、poster 促销海报"
    )
    caption: str | None = Field(default=None, max_length=200, description="海报文案或补充要求")
    count: int = Field(default=1, ge=1, le=4)
    platform: PlatformStyle | None = Field(
        default=None,
        description=(
            "平台风格：xiaohongshu 小红书风、douyin 抖音风、tmall 天猫京东大促风。"
            "用户点名平台风格时填写，比例自动跟随平台"
        ),
    )
    ratio: Ratio | None = Field(
        default=None,
        description=(
            "输出比例；缺省跟随平台风格（小红书 4:5、抖音 9:16、天猫京东 1:1），"
            "未选平台时 1:1"
        ),
    )
    from_previous: bool = Field(
        default=False,
        description="输入取上一步的产物而不是当前画布（如刚生成的海报）",
    )
    previous_index: int | None = Field(
        default=None, ge=1, description="上一步产出多张时用第几张"
    )
    source_asset_id: str | None = None  # 服务端解析注入，不暴露给模型


class PrepareDeliverySizesIn(BaseModel):
    ratios: list[Ratio] = Field(
        default_factory=lambda: list(DELIVERY_RATIOS),
        min_length=1,
        max_length=len(Ratio),
        description="要输出的投放比例。默认 1:1、4:5、9:16。主体不被裁切。",
    )
    prompt: str = Field(
        default="自然延伸画面边缘，保持主体完整，不要裁切商品",
        max_length=500,
    )
    from_previous: bool = Field(
        default=False,
        description="输入取上一步的产物而不是当前画布（如刚生成的海报）",
    )
    previous_index: int | None = Field(
        default=None, ge=1, description="上一步产出多张时用第几张"
    )
    source_asset_id: str | None = None  # 服务端解析注入，不暴露给模型


async def _store(
    session: AsyncSession,
    run: ToolRun,
    images: list[bytes],
    *,
    kind: AssetKind,
    variants: list[dict],
) -> dict:
    created = [
        await assets.create_from_bytes(session, run.user_id, data, kind, AssetSource.TOOL)
        for data in images
    ]
    labeled = []
    for asset, variant in zip(created, variants, strict=True):
        labeled.append({**variant, "asset_id": str(asset.id)})
    return {"asset_ids": [str(asset.id) for asset in created], "variants": labeled}


async def _progress(session: AsyncSession, run: ToolRun, progress: int, stage: str) -> None:
    await runs.report(session, run, progress, stage)


def _prompt_of(
    kind: MarketingKind, caption: str | None, platform: PlatformStyle | None = None
) -> str:
    parts = [_PROMPTS[kind]]
    if platform is not None:
        parts.append(_PLATFORM_PROMPTS[platform])
    text = " ".join(parts)
    extra = (caption or "").strip()
    return f"{text} 文案：{extra}" if extra else text


def _unique(ratios: list[Ratio]) -> list[Ratio]:
    seen: list[Ratio] = []
    for ratio in ratios:
        if ratio not in seen:
            seen.append(ratio)
    return seen


async def _step_source(session: AsyncSession, run: ToolRun, record) -> bytes:
    """步骤输入：解析出 source_asset_id 就读指定素材，否则读当前画布（AER-FR-003）。"""
    source_asset_id = run.params.get("source_asset_id")
    if source_asset_id:
        asset = await assets.get_for_user(session, run.user_id, uuid.UUID(source_asset_id))
        if asset is None:
            raise ToolError("引用的上一步产物已不存在")
        return await storage.get(asset.storage_key)
    return await flatten_session(session, record)


async def generate_marketing_exec(session: AsyncSession, run: ToolRun) -> dict:
    record = await require_session(session, run)
    kind = MarketingKind(run.params["kind"])
    raw_platform = run.params.get("platform")
    platform = PlatformStyle(raw_platform) if raw_platform else None
    raw_ratio = run.params.get("ratio")
    # 比例优先级：显式 ratio > 平台联动 > 1:1
    ratio = Ratio(raw_ratio) if raw_ratio else _PLATFORM_RATIOS.get(platform, Ratio.SQUARE)
    width, height = size_of(ratio)
    await runs.report(session, run, 15, "读取画布")
    source = await _step_source(session, run, record)
    await runs.report(session, run, 30, "生成营销图")
    images = await get_image_provider().edit(
        EditRequest(
            prompt=_prompt_of(kind, run.params.get("caption"), platform),
            image=source,
            count=run.params["count"],
            width=width,
            height=height,
        ),
        on_progress=lambda progress, stage: _progress(session, run, progress, stage),
    )
    fitted = [resize_to(data, width, height) for data in images]
    await runs.report(session, run, 95, "营销图已加入图片墙")
    variant = {"kind": kind.value, "ratio": ratio.value, "width": width, "height": height}
    if platform is not None:
        variant["platform"] = platform.value
    return await _store(
        session,
        run,
        fitted,
        kind=AssetKind.MARKETING,
        variants=[variant] * len(fitted),
    )


async def prepare_delivery_sizes_exec(session: AsyncSession, run: ToolRun) -> dict:
    record = await require_session(session, run)
    ratios = _unique([Ratio(value) for value in run.params["ratios"]])
    await runs.report(session, run, 20, "读取画布")
    source = await _step_source(session, run, record)
    images: list[bytes] = []
    variants: list[dict] = []
    total = len(ratios)
    for index, ratio in enumerate(ratios):
        width, height = size_of(ratio)
        await runs.report(session, run, 30 + int(60 * index / total), f"适配 {ratio.value}")
        images.append(letterbox(source, width, height))
        variants.append({"kind": "export", "ratio": ratio.value, "width": width, "height": height})
    await runs.report(session, run, 95, "投放尺寸已加入图片墙")
    return await _store(session, run, images, kind=AssetKind.EXPORT, variants=variants)


GENERATE_MARKETING = ToolSpec(
    name="generate_marketing",
    label="营销图",
    description=(
        "按当前画布生成电商营销图，只进图片墙，不改当前画布。"
        "kind：product 商品主图（白底）、scene 场景氛围图、"
        "model 模特上身、poster 促销海报。caption 可填海报标题或补充要求。"
        "用户点名平台风格（小红书风、抖音风、天猫京东大促风）时填 platform："
        "xiaohongshu / douyin / tmall，出图比例自动跟随平台，无需另填 ratio。"
        "用户说对刚生成的某张图（如上一张海报）继续处理时，加 from_previous=true。"
    ),
    params=GenerateMarketingIn,
    handler=generate_marketing_exec,
    queued=True,
    session_required=True,
    agent_hidden=("source_asset_id",),
)

PREPARE_DELIVERY_SIZES = ToolSpec(
    name="prepare_delivery_sizes",
    label="投放尺寸",
    description=(
        "把当前画布完整放入投放比例，不裁切、不改画面，只进图片墙。"
        "默认一次出 1:1、4:5、9:16。用户说改尺寸、出投放物料时用这个，不要用 crop_canvas。"
        "对刚生成的图（如上一张海报）出尺寸时加 from_previous=true；"
        "上一张产出多张时用 previous_index 指明第几张。"
    ),
    params=PrepareDeliverySizesIn,
    handler=prepare_delivery_sizes_exec,
    queued=False,
    session_required=True,
    agent_hidden=("source_asset_id",),
)
