import io
import json
import uuid
import zipfile

import httpx
import pytest

from app.tasks.tools import run_tool
from tests.test_sessions import upload


@pytest.fixture
async def signed_in(client: httpx.AsyncClient, credentials):
    await client.post("/api/auth/register", json=credentials)
    return client


async def start_batch(
    client: httpx.AsyncClient, asset_ids: list[str], operations: list[dict], **extra
):
    payload = {"asset_ids": asset_ids, "operations": operations, **extra}
    response = await client.post("/api/batches", json=payload)
    assert response.status_code == 202, response.text
    return response.json()


async def test_batch_remove_background_writes_export_assets(signed_in: httpx.AsyncClient):
    ids = [await upload(signed_in), await upload(signed_in, (400, 300))]
    run = await start_batch(
        signed_in, ids, [{"tool": "remove_background", "params": {}}], formats=["png"]
    )
    await run_tool({}, uuid.UUID(run["id"]))

    body = (await signed_in.get(f"/api/batches/{run['id']}")).json()
    assert body["run"]["status"] == "succeeded"
    assert len(body["items"]) == 2
    assert all(item["status"] == "succeeded" for item in body["items"])
    assert all(len(item["outputs"]) == 1 for item in body["items"])
    assert all(item["outputs"][0]["kind"] == "export" for item in body["items"])


async def test_batch_replace_background_sends_source_size(
    signed_in: httpx.AsyncClient, edit_requests: list
):
    asset_id = await upload(signed_in, (400, 300))
    run = await start_batch(
        signed_in, [asset_id], [{"tool": "replace_background", "params": {"prompt": "纯白"}}]
    )
    await run_tool({}, uuid.UUID(run["id"]))

    body = (await signed_in.get(f"/api/batches/{run['id']}")).json()
    assert body["run"]["status"] == "succeeded"
    assert len(edit_requests) == 1
    request = edit_requests[0]
    assert (request.width, request.height) == (400, 300)


async def test_batch_delivery_sizes_and_zip(signed_in: httpx.AsyncClient):
    asset_id = await upload(signed_in)
    run = await start_batch(
        signed_in,
        [asset_id],
        [{"tool": "prepare_delivery_sizes", "params": {}}],
        formats=["png"],
    )
    await run_tool({}, uuid.UUID(run["id"]))

    body = (await signed_in.get(f"/api/batches/{run['id']}")).json()
    sizes = {(item["width"], item["height"]) for item in body["items"][0]["outputs"]}
    assert sizes == {(1080, 1080), (1080, 1350), (1080, 1920)}

    packed = await signed_in.get(f"/api/batches/{run['id']}/export")
    assert packed.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(packed.content))
    assert len([name for name in archive.namelist() if name.startswith("images/")]) == 3


async def test_batch_rejects_unknown_tool(signed_in: httpx.AsyncClient):
    asset_id = await upload(signed_in)
    response = await signed_in.post(
        "/api/batches",
        json={
            "asset_ids": [asset_id],
            "operations": [{"tool": "split_layers", "params": {}}],
        },
    )
    assert response.status_code == 422


async def test_batch_rejects_foreign_asset(
    client: httpx.AsyncClient, credentials, other_credentials
):
    await client.post("/api/auth/register", json=credentials)
    asset_id = await upload(client)
    await client.post("/api/auth/logout")
    await client.post("/api/auth/register", json=other_credentials)

    response = await client.post(
        "/api/batches",
        json={"asset_ids": [asset_id], "operations": [{"tool": "remove_background"}]},
    )
    assert response.status_code == 404


async def test_batch_list_returns_recent_jobs(signed_in: httpx.AsyncClient):
    asset_id = await upload(signed_in)
    run = await start_batch(signed_in, [asset_id], [{"tool": "remove_background"}])
    await run_tool({}, uuid.UUID(run["id"]))

    body = (await signed_in.get("/api/batches")).json()
    assert body[0]["run"]["id"] == run["id"]


async def test_batch_replace_background_honors_count(
    signed_in: httpx.AsyncClient, edit_requests: list
):
    """AER-FR-011 / AC-021：批量换背景忠实执行 count，每源产出指定候选数。"""
    asset_id = await upload(signed_in, (400, 300))
    run = await start_batch(
        signed_in,
        [asset_id],
        [{"tool": "replace_background", "params": {"prompt": "纯白", "count": 4}}],
        formats=["png"],
    )
    await run_tool({}, uuid.UUID(run["id"]))

    body = (await signed_in.get(f"/api/batches/{run['id']}")).json()
    assert body["run"]["status"] == "succeeded"
    assert edit_requests[-1].count == 4
    assert len(body["items"][0]["outputs"]) == 4


async def test_batch_count_branches_into_followup_ops(signed_in: httpx.AsyncClient):
    """AER-AC-022 前半：多候选后的批量操作处理所有分支。"""
    asset_id = await upload(signed_in, (400, 300))
    run = await start_batch(
        signed_in,
        [asset_id],
        [
            {"tool": "replace_background", "params": {"prompt": "纯白", "count": 2}},
            {"tool": "adjust_image", "params": {"brightness": 0.2}},
        ],
        formats=["png"],
    )
    await run_tool({}, uuid.UUID(run["id"]))

    body = (await signed_in.get(f"/api/batches/{run['id']}")).json()
    assert body["run"]["status"] == "succeeded"
    assert len(body["items"][0]["outputs"]) == 2


async def test_batch_branch_budget_caps_runaway_frames(
    signed_in: httpx.AsyncClient, edit_requests: list
):
    """分支预算：连乘帧数将超上限时在该操作执行前拦下，不发起指数放大的请求。"""
    asset_id = await upload(signed_in, (400, 300))
    run = await start_batch(
        signed_in,
        [asset_id],
        [
            {"tool": "replace_background", "params": {"prompt": "纯白", "count": 4}},
            {"tool": "replace_background", "params": {"prompt": "浅灰", "count": 4}},
            {"tool": "replace_background", "params": {"prompt": "米色", "count": 4}},
        ],
        formats=["png"],
    )
    await run_tool({}, uuid.UUID(run["id"]))

    body = (await signed_in.get(f"/api/batches/{run['id']}")).json()
    assert body["run"]["status"] == "failed"
    assert "上限" in (body["run"]["error"] or "")
    # 预算在第三个操作执行前生效：1 + 4 + 16 = 21 次编辑请求封顶，而不是 85 次
    assert len(edit_requests) <= 21


async def test_batch_budget_allows_legit_branch_combos(signed_in: httpx.AsyncClient):
    """合法组合不受预算影响：4 候选 × 3 投放尺寸 = 12 帧正常完成。"""
    asset_id = await upload(signed_in, (400, 300))
    run = await start_batch(
        signed_in,
        [asset_id],
        [
            {"tool": "replace_background", "params": {"prompt": "纯白", "count": 4}},
            {"tool": "prepare_delivery_sizes", "params": {}},
        ],
        formats=["png"],
    )
    await run_tool({}, uuid.UUID(run["id"]))

    body = (await signed_in.get(f"/api/batches/{run['id']}")).json()
    assert body["run"]["status"] == "succeeded"
    assert len(body["items"][0]["outputs"]) == 12


async def test_batch_budget_allows_exact_cap_combination(signed_in: httpx.AsyncClient):
    """AC-024：4 候选 × 4 候选 × 2 尺寸 = 32 帧恰在上限内，正常完成不误伤。"""
    asset_id = await upload(signed_in, (400, 300))
    run = await start_batch(
        signed_in,
        [asset_id],
        [
            {"tool": "replace_background", "params": {"prompt": "纯白", "count": 4}},
            {"tool": "replace_background", "params": {"prompt": "浅灰", "count": 4}},
            {"tool": "prepare_delivery_sizes", "params": {"ratios": ["1:1", "9:16"]}},
        ],
        formats=["png"],
    )
    await run_tool({}, uuid.UUID(run["id"]))
    body = (await signed_in.get(f"/api/batches/{run['id']}")).json()
    assert body["run"]["status"] == "succeeded", body["run"].get("error")
    assert len(body["items"][0]["outputs"]) == 32


async def test_batch_budget_rejects_beyond_cap(
    signed_in: httpx.AsyncClient, edit_requests: list
):
    """AC-024：32 帧之后再排一次 4 候选 → 超限拒绝，且拒绝发生在该操作执行前。"""
    asset_id = await upload(signed_in, (400, 300))
    run = await start_batch(
        signed_in,
        [asset_id],
        [
            {"tool": "replace_background", "params": {"prompt": "纯白", "count": 4}},
            {"tool": "replace_background", "params": {"prompt": "浅灰", "count": 4}},
            {"tool": "prepare_delivery_sizes", "params": {"ratios": ["1:1", "9:16"]}},
            {"tool": "replace_background", "params": {"prompt": "米色", "count": 4}},
        ],
        formats=["png"],
    )
    await run_tool({}, uuid.UUID(run["id"]))
    body = (await signed_in.get(f"/api/batches/{run['id']}")).json()
    assert body["run"]["status"] == "failed"
    assert "上限" in (body["run"]["error"] or "")
    # 前三个操作照常执行（rb 每帧一次调用：1 + 4 = 5 次；尺寸为本地处理），第四次在执行前被拦
    assert len(edit_requests) == 5


async def test_session_batch_execute_rejects_foreign_ids(signed_in: httpx.AsyncClient):
    """AC-023 兜底：会话内直调批量（模拟恢复的持久计划/陈旧客户端）带别会话 ID，
    执行外壳在处理任何源之前拒绝；独立批量页（无会话）不受影响。"""
    from PIL import Image

    from tests.test_sessions import open_session

    def _png_bytes(size, color):
        buffer = io.BytesIO()
        Image.new("RGB", size, color).save(buffer, format="PNG")
        return buffer.getvalue()

    created_a = await open_session(signed_in, image=_png_bytes((300, 300), (0, 90, 200)))
    foreign = created_a["current_asset_id"]
    created_b = await open_session(signed_in, image=_png_bytes((280, 220), (200, 30, 30)))
    session_b = created_b["id"]

    response = await signed_in.post(
        f"/api/sessions/{session_b}/tools",
        json={
            "tool": "batch_process",
            "params": {
                "asset_ids": [foreign],
                "operations": [{"tool": "adjust_image", "params": {"brightness": 0.2}}],
            },
        },
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run"]["id"]
    await run_tool({}, uuid.UUID(run_id))

    body = (await signed_in.get(f"/api/runs/{run_id}")).json()
    assert body["status"] == "failed"
    assert "当前会话" in (body["error"] or "")


async def test_batch_zip_manifest_matches_outputs(signed_in: httpx.AsyncClient):
    """AER-AC-022 后半：zip 含 manifest，逐源列出文件与状态，与下载文件一致。"""
    asset_id = await upload(signed_in, (400, 300))
    run = await start_batch(
        signed_in,
        [asset_id],
        [{"tool": "replace_background", "params": {"prompt": "纯白", "count": 2}}],
        formats=["png"],
    )
    await run_tool({}, uuid.UUID(run["id"]))

    packed = await signed_in.get(f"/api/batches/{run['id']}/export")
    assert packed.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(packed.content))
    names = [name for name in archive.namelist() if name.startswith("images/")]
    assert len(names) == 2
    assert "manifest.json" in archive.namelist()

    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["items"][0]["status"] == "succeeded"
    assert manifest["items"][0]["source_id"] == asset_id
    assert manifest["items"][0]["files"] == names
