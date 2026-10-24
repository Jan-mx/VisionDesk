"""AER-FR-004/006：局部编辑发 ROI 裁图（可感知选区位置），换背景只应用一次变换。"""

import io
import uuid

import httpx
import pytest
from PIL import Image, ImageChops, ImageDraw

from app import storage
from app.db import SessionFactory
from app.models import Asset, EditSession
from app.tasks.tools import run_tool
from tests.canvas import invoke, scene, select
from tests.test_sessions import open_session, upload


def asymmetric(size=(320, 240)) -> bytes:
    """左绿右红 + 偏心黄块的强非对称图：翻转/选区错位都能被像素分辨。"""
    image = Image.new("RGB", size, (170, 40, 40))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, size[0] // 2, size[1]), fill=(40, 150, 70))
    draw.rectangle((60, 60, 140, 150), fill=(240, 220, 60))  # 偏左黄块（左选区目标）
    draw.rectangle((210, 40, 280, 120), fill=(30, 60, 200))  # 偏右蓝块（右选区目标）
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
async def signed_in(client: httpx.AsyncClient, credentials):
    await client.post("/api/auth/register", json=credentials)
    return client


@pytest.fixture
def passthrough_edit(monkeypatch):
    """替身 Provider：原样返回输入，隔离模型内容差异，只验证坐标空间。"""
    from app.providers.mock import MockImageProvider

    async def passthrough(self, request, on_progress=None):
        return [request.image]

    monkeypatch.setattr(MockImageProvider, "edit", passthrough)


async def layer_asset_bytes(asset_id: str) -> bytes:
    async with SessionFactory() as db:
        asset = await db.get(Asset, uuid.UUID(asset_id))
        return await storage.get(asset.storage_key)


async def flatten_of(session_body: dict) -> bytes:
    from app.tools.context import flatten_session

    async with SessionFactory() as db:
        record = await db.get(EditSession, uuid.UUID(session_body["id"]))
        return await flatten_session(db, record)


def pixel_diff_bbox(before: bytes, after: bytes) -> tuple | None:
    """RGB 差异包围盒：getbbox 在 RGBA 上只统计 alpha 通道，必须先转 RGB。"""
    return ImageChops.difference(
        Image.open(io.BytesIO(before)).convert("RGB"),
        Image.open(io.BytesIO(after)).convert("RGB"),
    ).getbbox()


def right_half_unchanged(source: bytes, output: bytes, width: int) -> None:
    before = Image.open(io.BytesIO(source)).convert("RGB")
    after = Image.open(io.BytesIO(output)).convert("RGB")
    assert after.size == before.size
    edge = int(width * 0.6)
    diff = ImageChops.difference(
        before.crop((edge, 0, before.width, before.height)),
        after.crop((edge, 0, after.width, after.height)),
    )
    assert diff.getbbox() is None


# ---- AER-FR-004：模型可感知选区（ROI 裁图）----


async def test_two_selections_produce_different_model_inputs(
    signed_in: httpx.AsyncClient, edit_requests: list
):
    """AC-007：同图同文字、不同选区 → 发往模型的有效输入可区分（两个独立会话隔离层写回）。"""
    results = []
    for point_x in (0.25, 0.75):
        session = await open_session(signed_in, image=asymmetric())
        selected = await select(
            signed_in, session["id"], session["revision"], points=[{"x": point_x, "y": 0.5}]
        )
        body = await invoke(
            signed_in,
            session["id"],
            "replace_region",
            {
                "prompt": "改成黑色",
                "mask_asset_id": selected["mask"]["id"],
                "revision": session["revision"],
            },
        )
        await run_tool({}, uuid.UUID(body["run"]["id"]))
        results.append(body)

    assert len(edit_requests) == 2
    assert edit_requests[0].image != edit_requests[1].image


async def test_region_request_is_cropped_not_whole_layer(
    signed_in: httpx.AsyncClient, edit_requests: list
):
    """有选区时发往模型的是裁剪区域，不再是整层原图。"""
    session = await open_session(signed_in, image=asymmetric())
    selected = await select(
        signed_in, session["id"], session["revision"], points=[{"x": 0.5, "y": 0.5}]
    )
    body = await invoke(
        signed_in,
        session["id"],
        "replace_region",
        {"prompt": "把黄块改成方形", "mask_asset_id": selected["mask"]["id"], "revision": session["revision"]},
    )
    await run_tool({}, uuid.UUID(body["run"]["id"]))

    request = edit_requests[0]
    assert (request.width, request.height) != (320, 240)
    assert 0 < request.width <= 320
    assert 0 < request.height <= 240


async def test_region_edit_keeps_pixels_outside_selection(signed_in: httpx.AsyncClient):
    """AC-008：选区（左侧）外的像素逐像素保持不变。"""
    source = asymmetric()
    session = await open_session(signed_in, image=source)
    selected = await select(
        signed_in, session["id"], session["revision"], points=[{"x": 0.25, "y": 0.5}]
    )
    body = await invoke(
        signed_in,
        session["id"],
        "replace_region",
        {"prompt": "改成黑色", "mask_asset_id": selected["mask"]["id"], "revision": session["revision"]},
    )
    await run_tool({}, uuid.UUID(body["run"]["id"]))
    updated = (await signed_in.get(f"/api/sessions/{session['id']}")).json()

    layer = updated["document"]["layers"][0]
    output = await layer_asset_bytes(layer["asset_id"])
    right_half_unchanged(source, output, 320)


async def test_region_edit_keeps_strict_zero_mask_pixels(signed_in: httpx.AsyncClient):
    """E2E-F05：原遮罩严格为零的每个像素逐字节不变（含选区边缘外环带）。

    此前断言只查远半区，羽化 blur 扩散出的 2-3px 环带逃过了检查——真实模型
    复现了 2,690 个零遮罩像素变化；本用例按遮罩逐像素严格核对，并确认域内确实变化。
    """
    from app.edits.mask import to_luma

    source = asymmetric()
    session = await open_session(signed_in, image=source)
    selected = await select(
        signed_in, session["id"], session["revision"], points=[{"x": 0.25, "y": 0.5}]
    )
    body = await invoke(
        signed_in,
        session["id"],
        "replace_region",
        {"prompt": "改成黑色", "mask_asset_id": selected["mask"]["id"], "revision": session["revision"]},
    )
    await run_tool({}, uuid.UUID(body["run"]["id"]))
    updated = (await signed_in.get(f"/api/sessions/{session['id']}")).json()

    layer = updated["document"]["layers"][0]
    output = await layer_asset_bytes(layer["asset_id"])
    before = Image.open(io.BytesIO(source)).convert("RGB")
    after = Image.open(io.BytesIO(output)).convert("RGB")
    assert after.size == before.size

    mask_bytes = await layer_asset_bytes(selected["mask"]["id"])
    luma = to_luma(mask_bytes, before.size)
    diff = ImageChops.difference(before, after)

    zero = luma.point(lambda v: 255 if v == 0 else 0)
    outside_diff = ImageChops.multiply(diff, Image.merge("RGB", (zero, zero, zero)))
    assert outside_diff.getbbox() is None, "原遮罩严格为零的像素被改动"

    inside = luma.point(lambda v: 255 if v > 0 else 0)
    inside_diff = ImageChops.multiply(diff, Image.merge("RGB", (inside, inside, inside)))
    assert inside_diff.getbbox() is not None  # 域内确有变化，防恒等假通过


# ---- AER-FR-006：换背景只应用一次变换 ----


async def test_replace_background_after_flip_applies_transform_once(
    signed_in: httpx.AsyncClient, passthrough_edit
):
    """AC-011：未拆层图片先翻转再单候选换背景，最终画布与换背景前逐像素一致。"""
    session = await open_session(signed_in, image=asymmetric())
    flipped = await invoke(signed_in, session["id"], "flip_layer", {"direction": "horizontal"})
    await run_tool({}, uuid.UUID(flipped["run"]["id"]))
    flipped_body = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    before = await flatten_of(flipped_body)

    body = await invoke(
        signed_in, session["id"], "replace_background", {"prompt": "纯白", "count": 1}
    )
    await run_tool({}, uuid.UUID(body["run"]["id"]))
    replaced = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    after = await flatten_of(replaced)

    assert replaced["current_asset_id"] != flipped_body["current_asset_id"]
    assert pixel_diff_bbox(before, after) is None


async def test_replace_background_after_crop_move_scale_rotate_keeps_composition(
    signed_in: httpx.AsyncClient, passthrough_edit
):
    """AC-012：裁剪+移动+缩放+旋转组合后的单层画布，换背景后构图逐像素一致。"""
    session = await open_session(signed_in, image=asymmetric())
    for tool, params in [
        ("crop_canvas", {"rect": {"x": 0.1, "y": 0.05, "width": 0.8, "height": 0.9}}),
        ("scale_layer", {"layer_id": "base", "factor": 1.4}),
        ("rotate_layer", {"layer_id": "base", "rotation": 30}),
        ("move_layer", {"layer_id": "base", "dx": -20, "dy": 15}),
    ]:
        body = await invoke(signed_in, session["id"], tool, params)
        await run_tool({}, uuid.UUID(body["run"]["id"]))
    moved = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    before = await flatten_of(moved)

    body = await invoke(
        signed_in, session["id"], "replace_background", {"prompt": "纯白", "count": 1}
    )
    await run_tool({}, uuid.UUID(body["run"]["id"]))
    replaced = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    after = await flatten_of(replaced)

    assert pixel_diff_bbox(before, after) is None
    # 重建后的文档是单层恒等变换：再次渲染不会重复应用旧变换
    layer = replaced["document"]["layers"][0]
    assert (layer["transform"]["scale_x"], layer["transform"]["rotation"]) == (1, 0)
