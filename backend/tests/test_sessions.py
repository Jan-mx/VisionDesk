import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from botocore.exceptions import ClientError
from sqlalchemy import func, select, text

from app import storage
from app.db import SessionFactory
from app.models import AgentRun, Asset, EditHistory, EditSession, SessionAsset
from tests.canvas import apply, invoke, scene
from tests.test_assets import make_image, upload_payload


@pytest.fixture
async def signed_in(client: httpx.AsyncClient, credentials):
    await client.post("/api/auth/register", json=credentials)
    return client


async def upload(client: httpx.AsyncClient, size=(320, 240), data: bytes | None = None) -> str:
    response = await client.post("/api/assets", files=upload_payload(data or make_image(size)))
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def open_session(client: httpx.AsyncClient, image: bytes | None = None, **overrides) -> dict:
    asset_id = await upload(client, data=image) if image else await upload(client)
    payload = {"current_asset_id": asset_id} | overrides
    response = await client.post("/api/sessions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def test_new_session_starts_at_first_revision(signed_in: httpx.AsyncClient):
    body = await open_session(signed_in, title="  白底 主图  ")

    assert body["title"] == "白底 主图"
    assert body["revision"] == 1
    assert body["original_asset_id"] == body["current_asset_id"]


async def test_document_describes_current_image_as_base_layer(signed_in: httpx.AsyncClient):
    asset_id = await upload(signed_in, (400, 500))

    body = await open_session(signed_in, current_asset_id=asset_id)

    assert (body["document"]["width"], body["document"]["height"]) == (400, 500)
    [layer] = body["document"]["layers"]
    assert (layer["kind"], layer["asset_id"], layer["locked"]) == ("image", asset_id, True)


async def test_blank_title_falls_back_to_placeholder(signed_in: httpx.AsyncClient):
    assert (await open_session(signed_in, title="   "))["title"] == "未命名会话"


async def test_unadopted_candidates_stay_on_the_wall(signed_in: httpx.AsyncClient):
    current = await upload(signed_in)
    others = [await upload(signed_in) for _ in range(2)]

    body = await open_session(signed_in, current_asset_id=current, asset_ids=[current, *others])

    assert [asset["id"] for asset in body["assets"]] == [current, *others]


async def test_switching_current_image_bumps_revision(signed_in: httpx.AsyncClient):
    body = await open_session(signed_in)
    other = await upload(signed_in, (500, 500))

    patched = (
        await signed_in.patch(f"/api/sessions/{body['id']}", json={"current_asset_id": other})
    ).json()

    assert patched["revision"] == 2
    assert patched["current_asset_id"] == other
    assert patched["document"]["width"] == 500
    assert other in [asset["id"] for asset in patched["assets"]]


async def test_switching_to_the_same_image_keeps_revision(signed_in: httpx.AsyncClient):
    body = await open_session(signed_in)

    patched = (
        await signed_in.patch(
            f"/api/sessions/{body['id']}", json={"current_asset_id": body["current_asset_id"]}
        )
    ).json()

    assert patched["revision"] == 1


async def test_picking_the_cropped_image_again_restores_its_full_size(signed_in: httpx.AsyncClient):
    """裁剪只缩小画布、不换资产，点回这张图应当回到原图画幅。"""
    session = await open_session(signed_in)
    cropped = (await invoke(signed_in, session["id"], "crop_canvas", {"ratio": "1:1"}))["session"]
    assert (cropped["document"]["width"], cropped["document"]["height"]) == (240, 240)

    patched = (
        await signed_in.patch(
            f"/api/sessions/{session['id']}",
            json={"current_asset_id": session["current_asset_id"]},
        )
    ).json()

    assert (patched["document"]["width"], patched["document"]["height"]) == (320, 240)
    assert patched["revision"] == cropped["revision"] + 1


async def test_history_records_creation_and_switches(signed_in: httpx.AsyncClient):
    body = await open_session(signed_in)
    other = await upload(signed_in)
    await signed_in.patch(f"/api/sessions/{body['id']}", json={"current_asset_id": other})

    entries = (await signed_in.get(f"/api/sessions/{body['id']}/history")).json()

    assert [entry["action"] for entry in entries] == ["switch_current", "create_session"]
    assert entries[0]["result"]["revision"] == 2


async def test_unknown_asset_is_rejected(signed_in: httpx.AsyncClient):
    response = await signed_in.post("/api/sessions", json={"current_asset_id": str(uuid.uuid4())})

    assert response.status_code == 404


async def test_session_requires_authentication(client: httpx.AsyncClient):
    assert (await client.get("/api/sessions")).status_code == 401
    assert (await client.get(f"/api/sessions/{uuid.uuid4()}")).status_code == 401


async def test_sessions_are_isolated_per_user(
    client: httpx.AsyncClient, credentials, other_credentials
):
    await client.post("/api/auth/register", json=credentials)
    session_id = (await open_session(client))["id"]

    await client.post("/api/auth/logout")
    await client.post("/api/auth/register", json=other_credentials)

    assert (await client.get(f"/api/sessions/{session_id}")).status_code == 404
    assert (await client.get("/api/sessions")).json() == []


# ---- 创意库 LIB：会话删除预览与删除（LIB-FR-004 / LIB-FR-013 / LIB-FR-015）----


async def deletion_preview(client: httpx.AsyncClient, session_id: str) -> dict:
    """GET /api/sessions/{id}/deletion-preview，返回 200 预览体。"""
    response = await client.get(f"/api/sessions/{session_id}/deletion-preview")
    assert response.status_code == 200, response.text
    return response.json()


async def confirm_delete(client: httpx.AsyncClient, session_id: str, token: str) -> httpx.Response:
    """携带 X-Deletion-Preview 请求头删除会话。"""
    return await client.delete(
        f"/api/sessions/{session_id}", headers={"X-Deletion-Preview": token}
    )


async def storage_key_of(asset_id: str) -> str:
    """删除前先从库里取 storage_key，供 S3 对象可读性断言使用。"""
    async with SessionFactory() as session:
        row = await session.get(Asset, uuid.UUID(asset_id))
    assert row is not None
    return row.storage_key


async def _count(model, **filters) -> int:
    async with SessionFactory() as session:
        return await session.scalar(select(func.count()).select_from(model).filter_by(**filters))


async def test_deletion_preview_counts_exclusive_and_shared_assets(signed_in: httpx.AsyncClient):
    """LIB-FR-015 / LIB-AC-004、014：预览计数来自全量 C(X)/D(X)，共用素材计入保留。"""
    exclusive = await upload(signed_in)
    shared = await upload(signed_in)
    other = await upload(signed_in)
    session_x = await open_session(
        signed_in, current_asset_id=exclusive, asset_ids=[shared], title="X"
    )
    await open_session(signed_in, current_asset_id=other, asset_ids=[shared], title="Y")

    preview = await deletion_preview(signed_in, session_x["id"])

    assert preview["session_id"] == session_x["id"]
    # exclusive 同时在墙、原图、当前图、图层与历史里出现，按素材 ID 去重只计 1
    assert preview["delete_asset_count"] == 1
    # shared 同时在 X 与 Y 的墙上，属于其他会话引用，必须保留
    assert preview["retained_asset_count"] == 1
    assert preview["blocked"] is False
    assert preview["blockers"] == []
    assert preview["preview_token"]
    expires = datetime.fromisoformat(preview["expires_at"])
    assert timedelta(0) < expires - datetime.now(UTC) <= timedelta(minutes=6)


async def test_deletion_preview_dedups_and_retains_working_kinds(signed_in: httpx.AsyncClient):
    """LIB-FR-015 / LIB-AC-014：拆层派生的 SUBJECT/BACKGROUND 属隐藏 kind，计入保留、不进删除。"""
    extra = await upload(signed_in)
    session = await open_session(signed_in, image=scene(), asset_ids=[extra])
    await apply(signed_in, session["id"], "split_layers")

    detail = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    working = {
        asset["id"]
        for asset in detail["assets"]
        if asset["kind"] in {"subject", "background"}
    }
    assert len(working) == 2

    preview = await deletion_preview(signed_in, session["id"])

    # 可删除：当前底图（原图+当前+图层+历史去重）与纯墙 extra
    assert preview["delete_asset_count"] == 2
    # 保留：subject / background 两个派生素材
    assert preview["retained_asset_count"] == 2
    assert preview["blocked"] is False


async def test_deletion_preview_ignores_session_list_limits(signed_in: httpx.AsyncClient):
    """LIB-FR-015 / LIB-AC-014：预览基于数据库全量，超出列表 limit 的旧会话引用仍计入保留。"""
    shared = await upload(signed_in)
    newest = None
    for _ in range(51):
        response = await signed_in.post("/api/sessions", json={"current_asset_id": shared})
        assert response.status_code == 201, response.text
        newest = response.json()

    preview = await deletion_preview(signed_in, newest["id"])

    # shared 被另外 50 个（超过默认列表 limit 的）旧会话墙引用，不允许连带清理
    assert preview["delete_asset_count"] == 0
    assert preview["retained_asset_count"] == 1
    assert preview["blocked"] is False


async def test_delete_session_cascades_and_keeps_shared_assets(signed_in: httpx.AsyncClient):
    """LIB-FR-004 / LIB-AC-003、004：确认删除后独占素材与对象一起清理；共用素材、隐藏派生保留；
    详情、历史、图片墙、对话记录级联清空。"""
    base = await upload(signed_in, data=scene())
    shared = await upload(signed_in)
    other = await upload(signed_in)
    session_x = await open_session(signed_in, current_asset_id=base, asset_ids=[shared], title="X")
    session_y = await open_session(signed_in, current_asset_id=other, asset_ids=[shared], title="Y")
    # X 里跑一个同步工具留下终态 ToolRun；再拆层得到 subject/background 派生素材
    await apply(signed_in, session_x["id"], "flip_layer", {"direction": "horizontal"})
    await apply(signed_in, session_x["id"], "split_layers")
    detail_x = (await signed_in.get(f"/api/sessions/{session_x['id']}")).json()
    working_ids = {
        asset["id"]
        for asset in detail_x["assets"]
        if asset["kind"] in {"subject", "background"}
    }
    assert len(working_ids) == 2
    # 直接落一条 AgentRun，验证对话记录随会话级联清空
    session_uuid = uuid.UUID(session_x["id"])
    async with SessionFactory() as session:
        owner = await session.scalar(
            select(EditSession.user_id).where(EditSession.id == session_uuid)
        )
        session.add(
            AgentRun(
                user_id=owner,
                session_id=session_uuid,
                revision=detail_x["revision"],
                goal="测试",
                plan=[],
                status="succeeded",
            )
        )
        await session.commit()

    base_key = await storage_key_of(base)
    shared_key = await storage_key_of(shared)
    working_keys = {asset_id: await storage_key_of(asset_id) for asset_id in working_ids}
    token = (await deletion_preview(signed_in, session_x["id"]))["preview_token"]

    response = await confirm_delete(signed_in, session_x["id"], token)

    assert response.status_code == 204
    assert (await signed_in.get(f"/api/sessions/{session_x['id']}")).status_code == 404
    # 独占素材：记录与对象双双消失
    assert (await signed_in.get(f"/api/assets/{base}")).status_code == 404
    with pytest.raises(ClientError):
        await storage.get(base_key)
    # 共用素材：Y 仍在使用，记录与对象都保留
    assert (await signed_in.get(f"/api/assets/{shared}")).status_code == 200
    assert await storage.get(shared_key)
    # 隐藏派生 kind：会话删除时不清理
    for asset_id, key in working_keys.items():
        assert (await signed_in.get(f"/api/assets/{asset_id}")).status_code == 200
        assert await storage.get(key)
    # 子表级联清空：历史、图片墙、对话记录
    assert await _count(EditHistory, session_id=session_uuid) == 0
    assert await _count(SessionAsset, session_id=session_uuid) == 0
    assert await _count(AgentRun, session_id=session_uuid) == 0
    # Y 不受影响，共用素材仍在 Y 墙上
    detail_y = (await signed_in.get(f"/api/sessions/{session_y['id']}")).json()
    assert shared in [asset["id"] for asset in detail_y["assets"]]


async def test_delete_session_rejects_missing_or_tampered_token(signed_in: httpx.AsyncClient):
    """LIB-FR-015：缺少或签名非法的 preview_token 一律 422，且零写入。"""
    session = await open_session(signed_in)

    missing = await signed_in.delete(f"/api/sessions/{session['id']}")
    assert missing.status_code == 422

    token = (await deletion_preview(signed_in, session["id"]))["preview_token"]
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    rejected = await confirm_delete(signed_in, session["id"], tampered)
    assert rejected.status_code == 422

    assert (await signed_in.get(f"/api/sessions/{session['id']}")).status_code == 200


async def test_delete_session_rejects_token_bound_to_other_session(signed_in: httpx.AsyncClient):
    """LIB-AC-014：token 绑定错误（属于其他会话）返回 422，双方零写入。"""
    session_x = await open_session(signed_in, title="X")
    session_y = await open_session(signed_in, title="Y")

    token_y = (await deletion_preview(signed_in, session_y["id"]))["preview_token"]
    response = await confirm_delete(signed_in, session_x["id"], token_y)

    assert response.status_code == 422
    assert (await signed_in.get(f"/api/sessions/{session_x['id']}")).status_code == 200
    assert (await signed_in.get(f"/api/sessions/{session_y['id']}")).status_code == 200


async def test_delete_session_rejects_expired_token(
    signed_in: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """LIB-AC-014：token 超过 5 分钟有效期返回 409 delete_preview_expired，零写入。

    preview token 的签发与校验都落在 app.security（WI 决策 3），
    冻结该模块时钟即可确定性地构造过期，不等待真实时间。
    """
    from app import security

    session = await open_session(signed_in)
    token = (await deletion_preview(signed_in, session["id"]))["preview_token"]

    future = datetime.now(UTC) + timedelta(minutes=6)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return future.astimezone(tz) if tz else future.replace(tzinfo=None)

    monkeypatch.setattr(security, "datetime", FrozenDatetime)
    response = await confirm_delete(signed_in, session["id"], token)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "delete_preview_expired"
    assert (await signed_in.get(f"/api/sessions/{session['id']}")).status_code == 200


async def test_delete_session_rejects_stale_preview_scope(signed_in: httpx.AsyncClient):
    """LIB-FR-015 / LIB-AC-014：预览后删除范围变化返回 409 delete_preview_changed，零写入。"""
    session = await open_session(signed_in)
    token = await deletion_preview(signed_in, session["id"])
    token = token["preview_token"]

    newcomer = await upload(signed_in)
    switched = await signed_in.patch(
        f"/api/sessions/{session['id']}", json={"current_asset_id": newcomer}
    )
    assert switched.status_code == 200

    response = await confirm_delete(signed_in, session["id"], token)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "delete_preview_changed"
    still = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    assert still["current_asset_id"] == newcomer


async def test_deletion_endpoints_require_authentication(client: httpx.AsyncClient):
    """LIB-FR-011：预览与删除均要求登录。"""
    session_id = uuid.uuid4()
    assert (
        await client.get(f"/api/sessions/{session_id}/deletion-preview")
    ).status_code == 401
    assert (await client.delete(f"/api/sessions/{session_id}")).status_code == 401


async def test_deletion_endpoints_isolated_per_user(
    client: httpx.AsyncClient, credentials, other_credentials
):
    """LIB-FR-011 / LIB-AC-008：非本人与不存在的会话对外表现相同；归属校验先于 token 校验。"""
    await client.post("/api/auth/register", json=credentials)
    session_id = (await open_session(client))["id"]
    await client.post("/api/auth/logout")
    await client.post("/api/auth/register", json=other_credentials)

    foreign = await client.get(f"/api/sessions/{session_id}/deletion-preview")
    missing = await client.get(f"/api/sessions/{uuid.uuid4()}/deletion-preview")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {
        "detail": {"code": "not_found", "message": "资源不存在"}
    }

    # 归属校验在 token 校验之前：非法 token 不改变 404 的外部表现
    ordered = await client.delete(
        f"/api/sessions/{session_id}", headers={"X-Deletion-Preview": "garbage"}
    )
    assert ordered.status_code == 404

    await client.post("/api/auth/login", json=credentials)
    assert (await client.get(f"/api/sessions/{session_id}")).status_code == 200


async def test_rename_session_folds_whitespace_and_truncates(signed_in: httpx.AsyncClient):
    """LIB-FR-003：会话重命名沿用既有空白折叠与 80 字符截断。"""
    session = await open_session(signed_in)

    patched = await signed_in.patch(
        f"/api/sessions/{session['id']}", json={"title": "  A " + "长" * 100 + "  B "}
    )

    assert patched.status_code == 200
    assert patched.json()["title"] == "A " + "长" * 78


async def test_session_list_ties_break_by_id_ascending(signed_in: httpx.AsyncClient):
    """LIB-FR-002（§7 稳定排序）：updated_at 相同时按 id 升序作为稳定次排序。"""
    ids = [(await open_session(signed_in))["id"] for _ in range(4)]
    async with SessionFactory() as session:
        await session.execute(
            text("UPDATE edit_sessions SET updated_at = now() WHERE id = ANY(:ids)"),
            {"ids": [str(item) for item in ids]},
        )
        await session.commit()

    listed = (await signed_in.get("/api/sessions")).json()

    assert [item["id"] for item in listed] == sorted(ids, key=uuid.UUID)

