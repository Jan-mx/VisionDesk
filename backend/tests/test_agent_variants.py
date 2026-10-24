"""AER-FR-001：同一源主体的多颜色变体——独立图层、同一源、可整体撤销。"""

import io
import uuid

import httpx
import pytest
from PIL import Image, ImageChops, ImageDraw

from app import storage
from app.db import SessionFactory
from app.models import Asset, EditSession
from app.tasks.tools import run_tool
from tests.canvas import layers, scene, settle
from tests.test_agent import fake_planner, send, signed_in, tool_calls
from tests.test_sessions import open_session


async def asset_bytes_of(asset_id: str) -> bytes:
    async with SessionFactory() as db:
        asset = await db.get(Asset, uuid.UUID(asset_id))
        return await storage.get(asset.storage_key)


async def start_variants(fake_planner, client, session_id, colors, *, layer_id="subject", confirm=True):
    from tests.test_agent import tool_call

    fake_planner(tool_call("create_color_variants", {"colors": colors, "layer_id": layer_id}))
    turn = await send(client, session_id, f"把主体变成{'、'.join(colors)}，各一个主体")
    if confirm and turn["status"] == "queued":
        turn = (
            await client.post(f"/api/sessions/{session_id}/messages/{turn['id']}/confirm")
        ).json()
    return turn


async def prepared_session(client):
    """拆层会话：background + subject（scene 居中椭圆主体）。"""
    session = await open_session(client, image=scene())
    body = await client.post(
        f"/api/sessions/{session['id']}/tools", json={"tool": "split_layers", "params": {}}
    )
    await run_tool({}, uuid.UUID(body.json()["run"]["id"]))
    return (await client.get(f"/api/sessions/{session['id']}")).json()


async def test_color_variants_creates_independent_layers_from_same_source(
    signed_in: httpx.AsyncClient, fake_planner, edit_requests: list
):
    """AC-001：三个颜色三个独立图层，同一源生成，原主体保留。"""
    session = await prepared_session(signed_in)
    subject_asset_before = layers(session)["subject"]["asset_id"]

    turn = await start_variants(fake_planner, signed_in, session["id"], ["红色", "蓝色", "黑色"])
    finished = await settle(signed_in, session["id"])

    assert finished["status"] == "succeeded", finished.get("error")
    updated = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    doc_layers = updated["document"]["layers"]

    variants = [layer for layer in doc_layers if layer["id"].startswith("variant-")]
    assert len(variants) == 3
    assert len({layer["asset_id"] for layer in variants}) == 3  # 独立素材
    assert layers(updated)["subject"]["asset_id"] == subject_asset_before  # 原主体不动
    names = {layer["name"] for layer in variants}
    assert any("红" in name for name in names)

    # 同一源：每次模型请求的输入都等于主体层原图，且互为独立请求（count=1）
    # （列表首项是 split_layers 的背景修补，变体请求取最后 3 个）
    subject_bytes = await asset_bytes_of(subject_asset_before)
    variant_requests = edit_requests[-3:]
    assert len(edit_requests) == 4
    for request in variant_requests:
        assert request.image == subject_bytes
        assert request.count == 1


async def test_single_step_variant_plan_waits_for_confirm(
    signed_in: httpx.AsyncClient, fake_planner
):
    """§6.1：含审批工具的单步计划先停在待确认，计划卡列出颜色（AC-001 前置）。"""
    session = await prepared_session(signed_in)
    from tests.test_agent import tool_call

    fake_planner(tool_call("create_color_variants", {"colors": ["红色", "蓝色"]}))
    turn = await send(signed_in, session["id"], "把主体变成红色和蓝色，各一个主体")

    assert turn["status"] == "queued"
    assert len(turn["steps"]) == 1
    assert turn["steps"][0]["status"] == "pending"
    assert "红" in turn["steps"][0]["label"] and "蓝" in turn["steps"][0]["label"]
    assert "确认" in turn["reply"]
    # 未确认前画布未变
    untouched = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    assert len(untouched["document"]["layers"]) == len(session["document"]["layers"])


async def test_color_variants_partial_failure_reports_missing(
    signed_in: httpx.AsyncClient, fake_planner, monkeypatch
):
    """AC-002：某色失败时准确展示部分结果与缺失项，不谎报完整交付。"""
    from app.providers.base import ProviderError
    from app.providers.mock import MockImageProvider

    calls = {"total": 0}
    original = MockImageProvider.edit

    async def flaky(self, request, on_progress=None):
        calls["total"] += 1
        if calls["total"] == 3:
            raise ProviderError("模型超时")
        return await original(self, request, on_progress)

    session = await prepared_session(signed_in)  # split 的模型调用不计入失败计数
    monkeypatch.setattr(MockImageProvider, "edit", flaky)

    turn = await start_variants(fake_planner, signed_in, session["id"], ["红色", "蓝色", "黑色"])
    finished = await settle(signed_in, session["id"])

    assert finished["status"] == "succeeded"  # 部分成功
    updated = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    variants = [l for l in updated["document"]["layers"] if l["id"].startswith("variant-")]
    assert len(variants) == 2

    run_body = (await signed_in.get(f"/api/runs/{finished['steps'][0]['run_id']}")).json()
    assert run_body["result"].get("missing_colors") == ["黑色"]  # 缺失项如实可见


async def test_color_variants_full_failure_fails(
    signed_in: httpx.AsyncClient, fake_planner, monkeypatch
):
    """全部颜色失败：任务失败可重试，不写层不留副本。"""
    from app.providers.base import ProviderError
    from app.providers.mock import MockImageProvider

    async def broken(self, request, on_progress=None):
        raise ProviderError("模型不可用")

    monkeypatch.setattr(MockImageProvider, "edit", broken)

    session = await prepared_session(signed_in)
    before_layers = len(session["document"]["layers"])
    turn = await start_variants(fake_planner, signed_in, session["id"], ["红色"])
    finished = await settle(signed_in, session["id"])

    assert finished["status"] == "failed"
    updated = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    assert len(updated["document"]["layers"]) == before_layers  # 未写层


async def test_color_variants_undo_rolls_back_all_and_redo_restores(
    signed_in: httpx.AsyncClient, fake_planner, edit_requests: list
):
    """AC-002：一次操作的新增图层与素材整体可撤销、可重做。"""
    session = await prepared_session(signed_in)
    turn = await start_variants(fake_planner, signed_in, session["id"], ["红色", "蓝色"])
    finished = await settle(signed_in, session["id"])
    assert finished["status"] == "succeeded"
    done = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    assert len([l for l in done["document"]["layers"] if l["id"].startswith("variant-")]) == 2

    undone = (await signed_in.post(f"/api/sessions/{session['id']}/undo")).json()
    assert not [l for l in undone["document"]["layers"] if l["id"].startswith("variant-")]
    assert len(undone["document"]["layers"]) == len(session["document"]["layers"])

    redone = (await signed_in.post(f"/api/sessions/{session['id']}/redo")).json()
    assert len([l for l in redone["document"]["layers"] if l["id"].startswith("variant-")]) == 2


# ---- E2E-F01：独立主体前提与透明产物 ----


async def test_variants_require_split_or_selection(
    signed_in: httpx.AsyncClient, fake_planner
):
    """未拆层且无选区：底图是完整场景，拒绝执行并给出可操作引导，不复制整场景。"""
    session = await open_session(signed_in, image=scene())
    turn = await start_variants(
        fake_planner, signed_in, session["id"], ["红色"], layer_id=None
    )
    finished = await settle(signed_in, session["id"])

    assert finished["status"] == "failed"
    error = finished.get("error") or ""
    assert "点选" in error or "拆分" in error
    updated = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    assert not [l for l in updated["document"]["layers"] if l["id"].startswith("variant-")]
    assert updated["revision"] == session["revision"]  # 画布未动


async def test_variants_cut_subject_from_selection(
    signed_in: httpx.AsyncClient, fake_planner, edit_requests: list
):
    """未拆层但已点选主体：选区裁出真主体切片，变体是透明独立图层而非整场景。"""
    from tests.canvas import select

    session = await open_session(signed_in, image=scene())
    selected = await select(
        signed_in, session["id"], session["revision"], points=[{"x": 0.5, "y": 0.5}]
    )
    turn = await start_variants(
        fake_planner, signed_in, session["id"], ["红色", "蓝色"], layer_id=None
    )
    finished = await settle(signed_in, session["id"])
    assert finished["status"] == "succeeded", finished.get("error")

    updated = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    variants = [l for l in updated["document"]["layers"] if l["id"].startswith("variant-")]
    assert len(variants) == 2
    for layer in variants:
        data = await asset_bytes_of(layer["asset_id"])
        image = Image.open(io.BytesIO(data)).convert("RGBA")
        alpha = image.getchannel("A")
        # 独立主体：不是整场景不透明图，主体外透明
        assert alpha.getextrema()[0] == 0, "变体必须保留主体外透明"
        assert image.size != (320, 240)  # 裁到选区包围盒附近而非整画布

    # 每色同一源：两次请求输入完全一致（选区裁出的主体切片）
    assert edit_requests[-1].image == edit_requests[-2].image
    assert edit_requests[-1].count == 1
    # 原底图素材未被改动
    assert layers(updated)["base"]["asset_id"] == session["current_asset_id"]


async def test_variants_reapply_subject_alpha(signed_in: httpx.AsyncClient, fake_planner):
    """拆层主体源：模型返回不透明画布时，变体重应用主体 alpha 保持透明背景。"""
    session = await prepared_session(signed_in)
    subject = layers(session)["subject"]
    turn = await start_variants(fake_planner, signed_in, session["id"], ["红色"])
    finished = await settle(signed_in, session["id"])
    assert finished["status"] == "succeeded", finished.get("error")

    updated = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    variant = next(l for l in updated["document"]["layers"] if l["id"].startswith("variant-"))
    data = await asset_bytes_of(variant["asset_id"])
    image = Image.open(io.BytesIO(data)).convert("RGBA")
    # mock 的编辑结果是全不透明画布；修复前变体 alpha 极值是 [255, 255]
    assert image.getchannel("A").getextrema()[0] == 0
    assert (image.width, image.height) == (subject["width"], subject["height"])


# ---- 三轮 AC-033：完整恢复主体 alpha（逐值，不是极值） ----


def soft_alpha_subject(size=(300, 300)) -> bytes:
    """透明底 + 中心不透明主体 + 软边环带含 1/16/32/128/254 等中间 alpha 值。"""
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((80, 80, 220, 220), fill=(40, 120, 200, 255))
    # 软边：主体边缘向外逐环降低 alpha，覆盖各档中间值
    for offset, alpha in enumerate((254, 128, 32, 16, 1), start=1):
        draw.ellipse(
            (80 - offset * 3, 80 - offset * 3, 220 + offset * 3, 220 + offset * 3),
            outline=(40, 120, 200, alpha),
            width=3,
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def test_variants_restore_soft_alpha_value_by_value(
    signed_in: httpx.AsyncClient, fake_planner
):
    """AC-033：变体 alpha 与源主体切片逐值相同（软边值不得变实或丢失）。"""
    session = await open_session(signed_in, image=soft_alpha_subject())
    turn = await start_variants(fake_planner, signed_in, session["id"], ["红色"], layer_id="base")
    finished = await settle(signed_in, session["id"])
    assert finished["status"] == "succeeded", finished.get("error")

    updated = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    variant = next(l for l in updated["document"]["layers"] if l["id"].startswith("variant-"))
    data = await asset_bytes_of(variant["asset_id"])

    # 源主体切片：与工具同一提取逻辑（透明底 + trim 裁剪）
    from app.tools.variants import _subject_of, _trim_box
    from app.layers import LayerDocument

    base = layers(session)["base"]
    doc = LayerDocument.model_validate(session["document"])
    source_layer = next(l for l in doc.layers if l.id == "base")
    source_bytes = await asset_bytes_of(base["asset_id"])
    from PIL import Image as _Image

    source = _Image.open(io.BytesIO(source_bytes)).convert("RGBA")
    subject = _subject_of(source, None, source_layer, (doc.width, doc.height))
    variant_img = _Image.open(io.BytesIO(data)).convert("RGBA")

    assert variant_img.size == subject.size
    diff = ImageChops.difference(
        subject.getchannel("A"), variant_img.getchannel("A")
    )
    assert diff.getbbox() is None, "变体 alpha 与源主体逐值不一致"
    # 域内确有变化（防恒等假通过）：主体不透明区的 RGB 与源不同
    rgb_diff = ImageChops.difference(
        subject.convert("RGB"), variant_img.convert("RGB")
    )
    assert rgb_diff.getbbox() is not None
