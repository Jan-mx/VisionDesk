import asyncio
import copy
import io
import json
import uuid
import zipfile
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.edits.pixels import adjust, encode, letterbox
from app.models import Asset, ToolRun
from app.models.asset import AssetKind, AssetSource
from app.models.tool_run import RunStatus
from app.providers import EditRequest, get_background_removal_provider, get_image_provider
from app.ratios import DELIVERY_RATIOS, Ratio, cover_size, size_of
from app.schemas.asset import AssetOut
from app.schemas.batch import BatchIn, BatchItemOut, BatchOpIn, BatchOut
from app.schemas.run import RunOut
from app.services import assets as asset_service
from app.services import background, runs
from app.services.exports import PackedExport
from app.services.images import probe
from app.tools.context import ToolError
from app.tools.enhance import ExpandCanvasIn, ReplaceBackgroundIn, UpscaleImageIn
from app.tools.marketing import PrepareDeliverySizesIn
from app.tools.retouch import AdjustIn


class UnknownBatchAsset(Exception):
    pass


class EmptyBatch(Exception):
    pass


def _unique_ratios(values: list[Ratio]) -> list[Ratio]:
    seen: list[Ratio] = []
    for ratio in values:
        if ratio not in seen:
            seen.append(ratio)
    return seen


async def apply_op(
    data: bytes,
    operation: BatchOpIn,
    *,
    session: AsyncSession | None = None,
    run: ToolRun | None = None,
    checkpoint_key: str = "batch",
) -> list[bytes]:
    if operation.tool == "remove_background":
        if session is None or run is None:
            return [await get_background_removal_provider().remove(data)]
        return [await background.remove(session, run, data, key=checkpoint_key)]
    if operation.tool == "adjust_image":
        params = AdjustIn.model_validate(operation.params).model_dump()
        params.pop("layer_id", None)
        values = {key: value for key, value in params.items() if value}
        return [await asyncio.to_thread(lambda: adjust(data, **values))]
    if operation.tool == "upscale_image":
        scale = UpscaleImageIn.model_validate(operation.params).scale
        return [await get_image_provider().upscale(data, scale)]
    if operation.tool == "replace_background":
        parsed = ReplaceBackgroundIn.model_validate(operation.params)
        meta = probe(data)
        return await get_image_provider().edit(
            EditRequest(
                prompt=f"只替换背景，保持主体、光线和边缘不变。新背景：{parsed.prompt}",
                image=data,
                count=parsed.count,
                negative_prompt=parsed.negative_prompt,
                width=meta.width,
                height=meta.height,
            )
        )
    if operation.tool == "expand_canvas":
        parsed = ExpandCanvasIn.model_validate(operation.params)
        meta = probe(data)
        width, height = cover_size(meta.width, meta.height, parsed.ratio)
        return await get_image_provider().edit(
            EditRequest(prompt=parsed.prompt, image=data, width=width, height=height)
        )
    if operation.tool == "prepare_delivery_sizes":
        parsed = PrepareDeliverySizesIn.model_validate(operation.params or {})
        ratios = _unique_ratios(parsed.ratios or list(DELIVERY_RATIOS))
        return [letterbox(data, *size_of(ratio)) for ratio in ratios]
    raise ValueError(f"批量不支持 {operation.tool}")


async def _store(session: AsyncSession, user_id: uuid.UUID, images: list[bytes]) -> list[Asset]:
    created = []
    for data in images:
        created.append(
            await asset_service.create_from_bytes(
                session, user_id, data, AssetKind.EXPORT, AssetSource.TOOL
            )
        )
    return created


MAX_BATCH_FRAMES = 32


def _expected_branches(operation) -> int:
    """该操作对每帧输入的输出数上界，用于分支预算预检。"""
    if isinstance(operation, dict):
        tool = operation.get("tool")
        params = operation.get("params") or {}
    else:
        tool = operation.tool
        params = operation.params or {}
    if tool == "replace_background":
        return max(1, int(params.get("count") or 1))
    if tool == "prepare_delivery_sizes":
        ratios = params.get("ratios")
        return len(set(ratios)) if ratios else 3
    return 1


async def process_asset(
    session: AsyncSession,
    run: ToolRun,
    source: Asset,
    payload: BatchIn,
    asset_index: int,
) -> list[Asset]:
    data = await storage.get(source.storage_key)
    frames = [data]
    for operation_index, operation in enumerate(payload.operations):
        # 分支预算预检：候选连乘将超上限时在该操作执行前拦下，避免指数放大的模型请求
        expected = _expected_branches(operation)
        if len(frames) * expected > MAX_BATCH_FRAMES:
            raise ValueError(
                f"批量分支产物将达到 {len(frames) * expected} 张，"
                f"超过上限 {MAX_BATCH_FRAMES} 张，请减少候选数或操作步骤"
            )
        next_frames: list[bytes] = []
        for frame_index, frame in enumerate(frames):
            next_frames.extend(
                await apply_op(
                    frame,
                    operation,
                    session=session,
                    run=run,
                    checkpoint_key=f"batch:{asset_index}:{operation_index}:{frame_index}",
                )
            )
        frames = next_frames
        if len(frames) > MAX_BATCH_FRAMES:
            raise ValueError(
                f"批量分支产物已达 {len(frames)} 张，超过上限 {MAX_BATCH_FRAMES} 张，"
                "请减少候选数或操作步骤"
            )
    encoded: list[bytes] = []
    for frame in frames:
        for fmt in payload.formats:
            encoded.append(encode(frame, fmt))
    return await _store(session, run.user_id, encoded)


def _blank_items(asset_ids: list[str]) -> list[dict]:
    return [
        {"source_id": asset_id, "status": "pending", "error": None, "output_ids": []}
        for asset_id in asset_ids
    ]


async def execute(session: AsyncSession, run: ToolRun) -> dict:
    payload = BatchIn.model_validate(run.params)
    if not payload.asset_ids:
        # UI 直发必有 id 列表；聊天路径由服务端解析——都不该以空目标执行
        raise ToolError("批量任务没有可处理的目标")
    items = _blank_items([str(asset_id) for asset_id in payload.asset_ids])
    total = len(items)
    await runs.report(session, run, 8, "准备批量任务", {"items": copy.deepcopy(items)})

    for index, item in enumerate(items):
        item["status"] = "running"
        await runs.report(
            session,
            run,
            10 + int(80 * index / total),
            f"处理第 {index + 1} / {total} 张",
            {"items": copy.deepcopy(items)},
        )
        source = await asset_service.get_for_user(
            session, run.user_id, uuid.UUID(item["source_id"])
        )
        if source is None:
            item["status"] = "failed"
            item["error"] = "素材不存在"
            continue
        try:
            outputs = await process_asset(session, run, source, payload, index)
        except Exception as exc:
            item["status"] = "failed"
            item["error"] = str(exc) or "处理失败"
            continue
        item["status"] = "succeeded"
        item["output_ids"] = [str(asset.id) for asset in outputs]
        await runs.report(
            session,
            run,
            10 + int(80 * (index + 1) / total),
            f"完成第 {index + 1} / {total} 张",
            {"items": copy.deepcopy(items)},
        )

    if all(item["status"] == "failed" for item in items):
        from app.tools.context import ToolError

        raise ToolError(items[0]["error"] or "全部处理失败")
    return {"items": items}


async def require_assets(
    session: AsyncSession, user_id: uuid.UUID, asset_ids: list[uuid.UUID]
) -> list[Asset]:
    found = []
    for asset_id in asset_ids:
        asset = await asset_service.get_for_user(session, user_id, asset_id)
        if asset is None:
            raise UnknownBatchAsset
        found.append(asset)
    return found


async def list_for_user(
    session: AsyncSession, user_id: uuid.UUID, limit: int = 20
) -> list[ToolRun]:
    result = await session.scalars(
        select(ToolRun)
        .where(ToolRun.user_id == user_id, ToolRun.tool == "batch_process")
        .order_by(ToolRun.created_at.desc())
        .limit(limit)
    )
    return list(result)


async def detail(session: AsyncSession, run: ToolRun) -> BatchOut:
    raw_items = (run.result or {}).get("items") or [
        {"source_id": str(asset_id), "status": "pending", "error": None, "output_ids": []}
        for asset_id in (run.params or {}).get("asset_ids") or []
    ]
    items: list[BatchItemOut] = []
    for raw in raw_items:
        source = await asset_service.get_for_user(session, run.user_id, uuid.UUID(raw["source_id"]))
        if source is None:
            continue
        outputs = []
        for output_id in raw.get("output_ids") or []:
            asset = await asset_service.get_for_user(session, run.user_id, uuid.UUID(output_id))
            if asset is not None:
                outputs.append(AssetOut.of(asset))
        items.append(
            BatchItemOut(
                source=AssetOut.of(source),
                status=raw.get("status") or "pending",
                error=raw.get("error"),
                outputs=outputs,
            )
        )
    return BatchOut(run=RunOut.of(run), items=items, created_at=run.created_at)


async def pack(session: AsyncSession, run: ToolRun) -> PackedExport:
    if run.status is not RunStatus.SUCCEEDED:
        raise EmptyBatch

    created_at = datetime.now(UTC)
    entries: list[dict] = []  # manifest 逐源条目：状态、错误与文件名一一对应
    picked: list[tuple[Asset, bytes, str]] = []
    index = 0
    for raw in (run.result or {}).get("items") or []:
        files: list[str] = []
        for output_id in raw.get("output_ids") or []:
            asset = await asset_service.get_for_user(session, run.user_id, uuid.UUID(output_id))
            if asset is None:
                continue
            index += 1
            fmt = asset.image_format.upper()
            ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}.get(fmt, "png")
            name = f"images/{index:02d}-{asset.width}x{asset.height}.{ext}"
            files.append(name)
            picked.append((asset, await storage.get(asset.storage_key), name))
        entries.append(
            {
                "source_id": raw.get("source_id"),
                "status": raw.get("status"),
                "error": raw.get("error"),
                "files": files,
            }
        )
    if not picked:
        raise EmptyBatch

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for _asset, data, name in picked:
            zipped.writestr(name, data)
        zipped.writestr(
            "manifest.json",
            json.dumps(
                {"created_at": created_at.isoformat(), "items": entries},
                ensure_ascii=False,
                indent=2,
            ),
        )
    filename = f"批量导出-{created_at.strftime('%Y%m%d')}.zip"
    return PackedExport(data=archive.getvalue(), filename=filename)
