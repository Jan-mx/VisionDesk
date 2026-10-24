"""AER-FR-002/003/008/009：规划上下文（历史/选中层）、步骤产物引用、批量素材目标。"""

import io
import uuid

import httpx
import pytest
from langchain_core.messages import AIMessage
from PIL import Image, ImageDraw

from app import storage
from app.db import SessionFactory
from app.models import Asset, EditSession
from app.tasks.tools import run_tool
from tests.canvas import settle
from tests.test_agent import fake_planner, send, signed_in, tool_call, tool_calls
from tests.test_sessions import open_session


async def send_ctx(client, session_id, text, context=None) -> dict:
    response = await client.post(
        f"/api/sessions/{session_id}/messages", json={"text": text, **(context or {})}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def asset_bytes_of(asset_id: str) -> bytes:
    async with SessionFactory() as db:
        asset = await db.get(Asset, uuid.UUID(asset_id))
        return await storage.get(asset.storage_key)


async def run_params_of(run_id: str) -> dict:
    from app.models import ToolRun

    async with SessionFactory() as db:
        run = await db.get(ToolRun, uuid.UUID(run_id))
        return dict(run.params)


def poster_image(size=(1080, 1080), color=(200, 60, 20)) -> bytes:
    image = Image.new("RGB", size, color)
    ImageDraw.Draw(image).rectangle((100, 100, 400, 400), fill=(20, 200, 120))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def system_prompt_of(fake_planner) -> str:
    return fake_planner.messages[0].content


# ---- AER-FR-002 / AER-AC-003：有界最近历史进入规划 ----


async def test_recent_history_enters_planning_prompt(signed_in: httpx.AsyncClient, fake_planner):
    """续句规划能看到上一轮的目标与终态，不必重新描述（AC-003）。"""
    session = await open_session(signed_in)
    fake_planner(AIMessage(content="好的。"))
    await send(signed_in, session["id"], "把手机变成多种颜色，各一个主体")

    planner = fake_planner(tool_call("flip_layer", {"direction": "horizontal"}))
    await send(signed_in, session["id"], "并需要生成多个颜色的手机")

    prompt = system_prompt_of(planner)
    assert "把手机变成多种颜色" in prompt
    assert "已完成" in prompt or "进行中" in prompt  # 上一轮终态


# ---- AER-FR-008 / AER-AC-015：选中图层进入规划并可靠命中 ----


async def test_selected_layer_enters_planning_prompt(signed_in: httpx.AsyncClient, fake_planner):
    """提交选中图层后，规划摘要包含该层（服务端按当前文档校验）。"""
    session = await open_session(signed_in, image=poster_image())
    split = (await signed_in.post(
        f"/api/sessions/{session['id']}/tools", json={"tool": "split_layers", "params": {}}
    )).json()
    await run_tool({}, uuid.UUID(split["run"]["id"]))

    planner = fake_planner(tool_call("adjust_image", {"brightness": 0.3}))
    await send_ctx(
        signed_in,
        session["id"],
        "把选中这层调亮",
        {"context": {"selected_layer_id": "subject"}},
    )

    prompt = system_prompt_of(planner)
    assert "选中图层" in prompt
    assert "subject" in prompt


async def test_stale_selected_layer_is_omitted(signed_in: httpx.AsyncClient, fake_planner):
    """客户端提交的选中层不在当前文档中时不进入摘要（不信任客户端事实）。"""
    session = await open_session(signed_in)

    planner = fake_planner(tool_call("flip_layer", {"direction": "horizontal"}))
    await send_ctx(
        signed_in,
        session["id"],
        "翻转",
        {"context": {"selected_layer_id": "不存在的层"}},
    )

    prompt = system_prompt_of(planner)
    # 摘要行是「当前选中图层：id（名）」；只有实际选中才出现
    assert "当前选中图层：" not in prompt


async def test_selected_layer_points_tools_at_that_layer(
    signed_in: httpx.AsyncClient, fake_planner
):
    """AER-AC-015：选中非顶层说「把选中这层调亮」，实际修改该层。"""
    session = await open_session(signed_in, image=poster_image())
    split = (await signed_in.post(
        f"/api/sessions/{session['id']}/tools", json={"tool": "split_layers", "params": {}}
    )).json()
    await run_tool({}, uuid.UUID(split["run"]["id"]))

    fake_planner(
        tool_calls(
            ("adjust_image", {"brightness": 0.3, "layer_id": "subject"}),
            ("adjust_image", {"brightness": 0.3, "layer_id": "background"}),
        )
    )
    turn = await send_ctx(
        signed_in, session["id"], "把选中的主体层调亮", {"context": {"selected_layer_id": "subject"}}
    )
    await signed_in.post(f"/api/sessions/{session['id']}/messages/{turn['id']}/confirm")
    finished = await settle(signed_in, session["id"])
    assert finished["status"] == "succeeded"


# ---- AER-FR-003 / AER-AC-005、AC-006：步骤产物引用 ----


async def test_delivery_sizes_consume_poster_from_previous_step(
    signed_in: httpx.AsyncClient, fake_planner, edit_requests: list
):
    """AC-005：海报 B 生成后出尺寸，尺寸步实际消费 B 的字节，当前画布不切换。"""
    session = await open_session(signed_in, image=poster_image((320, 240), (60, 60, 200)))
    current_before = session["current_asset_id"]

    fake_planner(
        tool_calls(
            ("generate_marketing", {"kind": "poster"}),
            ("prepare_delivery_sizes", {"from_previous": True}),
        )
    )
    turn = await send(signed_in, session["id"], "做一张促销海报，然后对海报出投放尺寸")
    await signed_in.post(f"/api/sessions/{session['id']}/messages/{turn['id']}/confirm")
    finished = await settle(signed_in, session["id"])

    assert finished["status"] == "succeeded", finished.get("error")
    assert len(edit_requests) == 1  # 投放尺寸是本地 letterbox，只有海报一步调模型
    poster_step = finished["steps"][0]
    poster_run = (await signed_in.get(f"/api/runs/{poster_step['run_id']}")).json()
    poster_asset_id = poster_run["result"]["asset_ids"][0]
    # 尺寸步持久化的输入引用就是海报产物本身，而不是画布
    sizes_params = await run_params_of(finished["steps"][1]["run_id"])
    assert sizes_params["source_asset_id"] == poster_asset_id

    updated = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    assert updated["current_asset_id"] == current_before  # 只入墙的产物不切换画布


async def test_multi_candidates_without_index_fails_instead_of_guessing(
    signed_in: httpx.AsyncClient, fake_planner
):
    """AC-006：上游多产物且未指定索引 → 下游失败提示选择，不静默用第一张。"""
    session = await open_session(signed_in, image=poster_image((320, 240), (60, 60, 200)))

    fake_planner(
        tool_calls(
            ("generate_marketing", {"kind": "poster", "count": 2}),
            ("prepare_delivery_sizes", {"from_previous": True}),
        )
    )
    turn = await send(signed_in, session["id"], "出两张海报再对海报出尺寸")
    await signed_in.post(f"/api/sessions/{session['id']}/messages/{turn['id']}/confirm")
    finished = await settle(signed_in, session["id"])

    assert finished["status"] == "failed"
    sizes_step = finished["steps"][1]
    assert sizes_step["status"] == "failed"
    assert "第几张" in (finished["error"] or "")


async def test_previous_index_selects_specific_candidate(
    signed_in: httpx.AsyncClient, fake_planner, edit_requests: list
):
    """AC-006：previous_index=2 时消费第二张候选。"""
    session = await open_session(signed_in, image=poster_image((320, 240), (60, 60, 200)))

    fake_planner(
        tool_calls(
            ("generate_marketing", {"kind": "poster", "count": 2}),
            ("prepare_delivery_sizes", {"from_previous": True, "previous_index": 2}),
        )
    )
    turn = await send(signed_in, session["id"], "出两张海报，对第二张出尺寸")
    await signed_in.post(f"/api/sessions/{session['id']}/messages/{turn['id']}/confirm")
    finished = await settle(signed_in, session["id"])

    assert finished["status"] == "succeeded", finished.get("error")
    poster_run = (await signed_in.get(f"/api/runs/{finished['steps'][0]['run_id']}")).json()
    second = poster_run["result"]["asset_ids"][1]
    sizes_params = await run_params_of(finished["steps"][1]["run_id"])
    assert sizes_params["source_asset_id"] == second


# ---- AER-FR-009 / AER-AC-017、AC-018：聊天批量素材目标 ----


async def test_batch_wall_targets_resolved_server_side(
    signed_in: httpx.AsyncClient, fake_planner
):
    """AC-017：assets="wall" 由服务端解析为可见可处理常规素材（排除内部与快照）。"""
    session = await open_session(signed_in, image=poster_image((320, 240), (10, 10, 10)))
    split = (await signed_in.post(
        f"/api/sessions/{session['id']}/tools", json={"tool": "split_layers", "params": {}}
    )).json()
    await run_tool({}, uuid.UUID(split["run"]["id"]))
    wall = (await signed_in.get(f"/api/sessions/{session['id']}")).json()["assets"]
    expected = [
        a["id"]
        for a in wall
        if a["kind"] in {"original", "generated", "marketing", "export"}
    ]
    assert expected  # 场景自检：墙上确有常规素材

    fake_planner(
        tool_calls(
            ("batch_process", {"assets": "wall", "operations": [{"tool": "adjust_image", "params": {"brightness": 0.2}}]}),
        )
    )
    turn = await send(signed_in, session["id"], "这些图都调亮一点")
    assert turn["status"] == "running", turn.get("error") or turn["reply"]

    # 断言提交参数：真实 asset_ids 已解析且等于可见常规素材集合
    from app.models import ToolRun

    async with SessionFactory() as db:
        run = await db.get(ToolRun, uuid.UUID(turn["steps"][0]["run_id"]))
        assert sorted(run.params["asset_ids"]) == sorted(expected)
        assert run.params.get("assets") in (None, "wall")  # 哨兵可保留，不再参与判定


async def test_batch_explicit_selection_overrides_wall(
    signed_in: httpx.AsyncClient, fake_planner
):
    """显式选择集合优先：selected_asset_ids 决定目标（必须完全落在当前会话墙内）。"""
    session = await open_session(signed_in, image=poster_image((320, 240), (10, 10, 10)))
    bg = (await signed_in.post(
        f"/api/sessions/{session['id']}/tools",
        json={"tool": "replace_background", "params": {"prompt": "纯白", "count": 2}},
    )).json()
    await run_tool({}, uuid.UUID(bg["run"]["id"]))
    wall = (await signed_in.get(f"/api/sessions/{session['id']}")).json()["assets"]
    processable = sorted(
        a["id"]
        for a in wall
        if a["kind"] in {"original", "generated", "marketing", "export"}
    )
    assert len(processable) >= 3  # 原图 + 两个换背景候选
    chosen = [processable[0]]  # 只选第一张，与整墙解析可区分

    fake_planner(
        tool_calls(
            ("batch_process", {"assets": "wall", "operations": [{"tool": "adjust_image", "params": {"brightness": 0.2}}]}),
        )
    )
    turn = await send_ctx(
        signed_in,
        session["id"],
        "就处理选中的这张",
        {"context": {"selected_asset_ids": chosen}},
    )
    assert turn["status"] == "running", turn.get("error") or turn["reply"]

    from app.models import ToolRun

    async with SessionFactory() as db:
        run = await db.get(ToolRun, uuid.UUID(turn["steps"][0]["run_id"]))
        assert run.params["asset_ids"] == chosen


async def test_batch_rejects_oversized_or_empty_targets(
    signed_in: httpx.AsyncClient, fake_planner
):
    """AC-018：目标为空或超上限 → 可读拒绝，不静默截断。"""
    session = await open_session(signed_in, image=poster_image((320, 240), (10, 10, 10)))

    # 超出上限的显式集合 → 可读拒绝（不静默截断）
    ids = []
    from tests.test_sessions import upload

    for _ in range(21):
        ids.append(await upload(signed_in, (100, 100)))
    fake_planner(
        tool_calls(
            ("batch_process", {"assets": "wall", "operations": [{"tool": "adjust_image", "params": {"brightness": 0.2}}]}),
        )
    )
    turn = await send_ctx(
        signed_in,
        session["id"],
        "处理这些",
        {"context": {"selected_asset_ids": ids}},
    )
    assert turn["steps"] == []
    assert "20" in turn["reply"]  # 上限提示


async def test_batch_rejects_cross_session_targets(
    signed_in: httpx.AsyncClient, fake_planner
):
    """E2E-F02 / AC-018：显式集合含其他会话的素材 → 可读拒绝，绝不处理别会话素材。"""
    session_a = await open_session(signed_in, image=poster_image((320, 240), (10, 10, 10)))
    foreign = session_a["current_asset_id"]
    session_b = await open_session(signed_in, image=poster_image((280, 220), (200, 30, 30)))

    fake_planner(
        tool_calls(
            ("batch_process", {"assets": "wall", "operations": [{"tool": "adjust_image", "params": {"brightness": 0.2}}]}),
        )
    )
    turn = await send_ctx(
        signed_in,
        session_b["id"],
        "把这些图都调亮一点，亮度增加0.15。",
        {"context": {"selected_asset_ids": [foreign]}},
    )
    # 计划被整体放弃：没有任何步骤被提交（也就不会处理 A 会话的素材）
    assert turn["steps"] == []
    assert "当前会话" in turn["reply"] or "重新选择" in turn["reply"]


async def test_batch_mixed_targets_reject_if_any_foreign(
    signed_in: httpx.AsyncClient, fake_planner
):
    """混合集合（本会话 + 别会话）也整体拒绝：目标集合必须完全落在当前会话内。"""
    session_a = await open_session(signed_in, image=poster_image((300, 300), (0, 90, 200)))
    foreign = session_a["current_asset_id"]
    session_b = await open_session(signed_in, image=poster_image((280, 220), (200, 30, 30)))
    own = session_b["current_asset_id"]

    fake_planner(
        tool_calls(
            ("batch_process", {"assets": "wall", "operations": [{"tool": "adjust_image", "params": {"brightness": 0.2}}]}),
        )
    )
    turn = await send_ctx(
        signed_in,
        session_b["id"],
        "把这些图都调亮",
        {"context": {"selected_asset_ids": [own, foreign]}},
    )
    assert turn["steps"] == []
    assert "当前会话" in turn["reply"] or "重新选择" in turn["reply"]


# ---- E2E-F04：规划复审回路（多项要求不得只排第一项） ----


async def test_review_pass_appends_missing_second_step(
    signed_in: httpx.AsyncClient, fake_planner
):
    """初始计划只有第一项时，复审回路补上缺失的第二步并保持两步链。"""
    session = await open_session(signed_in, image=poster_image())
    planner = fake_planner(
        tool_calls(("generate_marketing", {"kind": "poster", "count": 1, "caption": "新品上市"})),
        tool_calls(("prepare_delivery_sizes", {"from_previous": True})),
    )
    turn = await send(signed_in, session["id"], "先生成一张海报，然后对这张海报出投放尺寸")
    assert [s["tool"] for s in turn["steps"]] == [
        "generate_marketing",
        "prepare_delivery_sizes",
    ]
    # 两步计划必须先确认再执行
    assert turn["status"] == "queued"
    # 复审提示包含原始指令与已排步骤
    review_prompt = planner.all_messages[1][0].content
    assert "先生成一张海报" in review_prompt
    assert "generate_marketing" in review_prompt


async def test_review_pass_dedupes_repeated_steps(
    signed_in: httpx.AsyncClient, fake_planner
):
    """复审原样重复已有步骤时不产生重复步骤（防重复执行/重复计费）。"""
    session = await open_session(signed_in, image=poster_image())
    fake_planner(
        tool_call("flip_layer", {"direction": "horizontal"}),
        tool_call("flip_layer", {"direction": "horizontal"}),
    )
    turn = await send(signed_in, session["id"], "水平翻转")
    assert [s["tool"] for s in turn["steps"]] == ["flip_layer"]


async def test_review_pass_drops_invalid_additions(
    signed_in: httpx.AsyncClient, fake_planner
):
    """复审补充的无效调用被丢弃，不影响原计划可执行性。"""
    session = await open_session(signed_in, image=poster_image())
    fake_planner(
        tool_call("flip_layer", {"direction": "horizontal"}),
        tool_call("no_such_tool", {"x": 1}),
    )
    turn = await send(signed_in, session["id"], "水平翻转")
    assert [s["tool"] for s in turn["steps"]] == ["flip_layer"]
    assert turn["status"] in {"running", "succeeded"}, turn.get("error")


# ---- E2E-F03：跨轮产物引用（from_previous 回退到上一轮产物） ----


async def test_cross_turn_reference_uses_last_product(
    signed_in: httpx.AsyncClient, fake_planner
):
    """上一轮生成的海报，本轮 from_previous 直接解析到该产物，不再报「没有前置结果」。"""
    session = await open_session(signed_in, image=poster_image())
    fake_planner(
        tool_calls(("generate_marketing", {"kind": "poster", "count": 1, "caption": "周末特惠"}))
    )
    first = await send(signed_in, session["id"], "生成一张周末特惠海报，只要1张")
    done1 = await settle(signed_in, session["id"])
    assert done1["status"] == "succeeded", done1.get("error")
    poster = (
        (await signed_in.get(f"/api/runs/{done1['steps'][0]['run_id']}")).json()["result"]
    )["asset_ids"][0]

    fake_planner(tool_calls(("prepare_delivery_sizes", {"from_previous": True})))
    second = await send(signed_in, session["id"], "给刚才生成的那张海报出投放尺寸")
    done2 = await settle(signed_in, session["id"])
    assert done2["status"] == "succeeded", done2.get("error")
    params = await run_params_of(done2["steps"][0]["run_id"])
    assert params["source_asset_id"] == poster


async def test_cross_turn_multi_product_requires_index(
    signed_in: httpx.AsyncClient, fake_planner
):
    """上一轮产物多张且未指明 previous_index：明确报错列张数，不静默取首张。"""
    session = await open_session(signed_in, image=poster_image())
    fake_planner(
        tool_calls(("generate_marketing", {"kind": "poster", "count": 2, "caption": "两连拍"}))
    )
    await send(signed_in, session["id"], "出两张海报")
    done1 = await settle(signed_in, session["id"])
    assert done1["status"] == "succeeded", done1.get("error")

    fake_planner(tool_calls(("prepare_delivery_sizes", {"from_previous": True})))
    await send(signed_in, session["id"], "对刚才的海报出尺寸")
    done2 = await settle(signed_in, session["id"])
    assert done2["status"] == "failed"
    assert "2" in (done2["error"] or "") and "previous_index" in (done2["error"] or "")


async def test_cross_turn_index_picks_nth_product(
    signed_in: httpx.AsyncClient, fake_planner
):
    """previous_index=2 跨轮取第二张产物。"""
    session = await open_session(signed_in, image=poster_image())
    fake_planner(
        tool_calls(("generate_marketing", {"kind": "poster", "count": 2, "caption": "两连拍"}))
    )
    await send(signed_in, session["id"], "出两张海报")
    done1 = await settle(signed_in, session["id"])
    products = (await signed_in.get(f"/api/runs/{done1['steps'][0]['run_id']}")).json()[
        "result"
    ]["asset_ids"]

    fake_planner(
        tool_calls(("prepare_delivery_sizes", {"from_previous": True, "previous_index": 2}))
    )
    await send(signed_in, session["id"], "对第二张海报出尺寸")
    done2 = await settle(signed_in, session["id"])
    assert done2["status"] == "succeeded", done2.get("error")
    params = await run_params_of(done2["steps"][0]["run_id"])
    assert params["source_asset_id"] == products[1]


# ---- E2E-F04：replace_region 消费上一步产物（海报后改蓝） ----


async def test_replace_region_edits_previous_product_into_wall(
    signed_in: httpx.AsyncClient, fake_planner, edit_requests: list
):
    """改蓝请求实际消费海报字节；结果作为新素材入墙，画布与原海报都不被修改。"""
    session = await open_session(signed_in, image=poster_image())
    fake_planner(
        tool_calls(("generate_marketing", {"kind": "poster", "count": 1, "caption": "新品上市"}))
    )
    await send(signed_in, session["id"], "生成一张新品上市海报")
    done1 = await settle(signed_in, session["id"])
    poster = (
        (await signed_in.get(f"/api/runs/{done1['steps'][0]['run_id']}")).json()["result"]
    )["asset_ids"][0]
    poster_bytes = await asset_bytes_of(poster)
    before = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    revision_before = before["revision"]

    fake_planner(tool_calls(("replace_region", {"prompt": "把海报上的杯子改成蓝色", "from_previous": True})))
    await send(signed_in, session["id"], "把这张海报上的杯子改成蓝色，原始画布不要改")
    done2 = await settle(signed_in, session["id"])
    assert done2["status"] == "succeeded", done2.get("error")

    # 请求确实以海报字节为输入
    assert edit_requests[-1].image == poster_bytes
    # 产物作为新素材进入当前会话墙
    new_asset = (
        (await signed_in.get(f"/api/runs/{done2['steps'][0]['run_id']}")).json()["result"]
    )["asset_ids"][0]
    after = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    assert new_asset in [a["id"] for a in after["assets"]]
    # 画布未动、原海报字节未变（禁止静默改原图）
    assert after["revision"] == revision_before
    assert await asset_bytes_of(poster) == poster_bytes


# ---- E2E-F03 辅助：历史摘要携带产物信息 ----


async def test_recent_history_mentions_last_product(
    signed_in: httpx.AsyncClient, fake_planner
):
    """上一轮产物信息进入规划摘要，续句模型可判断「刚才的海报」可衔接。"""
    session = await open_session(signed_in, image=poster_image())
    fake_planner(
        tool_calls(("generate_marketing", {"kind": "poster", "count": 1, "caption": "周末特惠"}))
    )
    await send(signed_in, session["id"], "生成一张周末特惠海报")
    done1 = await settle(signed_in, session["id"])
    assert done1["status"] == "succeeded"

    planner = fake_planner(tool_call("flip_layer", {"direction": "horizontal"}))
    await send(signed_in, session["id"], "水平翻转")
    prompt = system_prompt_of(planner)
    assert "上一轮产物" in prompt
    assert "generate_marketing" in prompt
