import io
import logging
import uuid

import httpx
import pytest
from botocore.exceptions import ClientError
from PIL import Image
from sqlalchemy import text

from app import storage
from app.db import SessionFactory
from app.models import Asset, SessionAsset
from app.services.images import ImageRejected, probe


def make_image(size=(256, 256), mode="RGB", fmt="PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, "white").save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture
async def signed_in(client: httpx.AsyncClient, credentials):
    await client.post("/api/auth/register", json=credentials)
    return client


def upload_payload(data: bytes, name="a.png", content_type="image/png"):
    return {"file": (name, data, content_type)}


async def test_upload_returns_metadata_and_signed_url(signed_in: httpx.AsyncClient):
    response = await signed_in.post("/api/assets", files=upload_payload(make_image((320, 200))))

    assert response.status_code == 201
    body = response.json()
    assert (body["width"], body["height"]) == (320, 200)
    assert body["image_format"] == "PNG"
    assert body["kind"] == "original"
    assert "X-Amz-Signature" in body["url"]


async def test_upload_detects_alpha_channel(signed_in: httpx.AsyncClient):
    response = await signed_in.post("/api/assets", files=upload_payload(make_image(mode="RGBA")))

    assert response.json()["has_alpha"] is True


async def test_upload_rejects_corrupted_file(signed_in: httpx.AsyncClient):
    response = await signed_in.post("/api/assets", files=upload_payload(b"not-an-image"))

    assert response.status_code == 422


async def test_upload_rejects_unsupported_format(signed_in: httpx.AsyncClient):
    gif = make_image(fmt="GIF")

    response = await signed_in.post("/api/assets", files=upload_payload(gif, "a.gif", "image/gif"))

    assert response.status_code == 422


async def test_upload_ignores_spoofed_extension(signed_in: httpx.AsyncClient):
    """格式以解码结果为准：JPEG 内容伪装成 .png 仍应按 JPEG 记录。"""
    response = await signed_in.post(
        "/api/assets", files=upload_payload(make_image(fmt="JPEG"), "a.png", "image/png")
    )

    assert response.json()["image_format"] == "JPEG"


async def test_upload_requires_authentication(client: httpx.AsyncClient):
    response = await client.post("/api/assets", files=upload_payload(make_image()))

    assert response.status_code == 401


async def test_library_groups_assets_by_session_and_hides_masks(signed_in: httpx.AsyncClient):
    loose = await signed_in.post("/api/assets", files=upload_payload(make_image((320, 200))))
    session = await signed_in.post(
        "/api/sessions", json={"current_asset_id": loose.json()["id"], "title": "主图"}
    )
    extra = await signed_in.post("/api/assets", files=upload_payload(make_image((400, 300))))
    await signed_in.post("/api/assets", files=upload_payload(make_image((256, 256), mode="RGBA")))
    await signed_in.post(
        f"/api/sessions/{session.json()['id']}/selection",
        json={"revision": 1, "points": [{"x": 0.5, "y": 0.5}]},
    )

    groups = (await signed_in.get("/api/assets/library")).json()

    assert [group["title"] for group in groups] == ["主图", "未归入会话"]
    assert groups[0]["session_id"] == session.json()["id"]
    assert extra.json()["id"] in [asset["id"] for asset in groups[1]["assets"]]
    assert all(asset["kind"] != "mask" for group in groups for asset in group["assets"])


async def test_assets_are_isolated_per_user(
    client: httpx.AsyncClient, credentials, other_credentials
):
    await client.post("/api/auth/register", json=credentials)
    created = await client.post("/api/assets", files=upload_payload(make_image()))
    asset_id = created.json()["id"]

    await client.post("/api/auth/logout")
    await client.post("/api/auth/register", json=other_credentials)

    assert (await client.get(f"/api/assets/{asset_id}")).status_code == 404
    assert (await client.get("/api/assets")).json() == []


def test_probe_rejects_tiny_image():
    with pytest.raises(ImageRejected):
        probe(make_image((16, 16)))


# ---- 创意库 LIB：素材重命名 / 单删 / 批删（LIB-FR-006、007、008、011）----


async def upload_asset(client: httpx.AsyncClient, size=(256, 256)) -> str:
    """上传一张普通素材并返回 id。"""
    response = await client.post("/api/assets", files=upload_payload(make_image(size)))
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def open_session(client: httpx.AsyncClient, **overrides) -> dict:
    """本文件内的会话创建助手（test_sessions 反向依赖本文件，不能互相导入）。"""
    payload = {"current_asset_id": await upload_asset(client)} | overrides
    response = await client.post("/api/sessions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def storage_key_of(asset_id: str) -> str:
    """删除前先从库里取 storage_key，供 S3 对象可读性断言使用。"""
    async with SessionFactory() as session:
        row = await session.get(Asset, uuid.UUID(asset_id))
    assert row is not None
    return row.storage_key


async def rename(client: httpx.AsyncClient, asset_id: str, title) -> httpx.Response:
    return await client.patch(f"/api/assets/{asset_id}", json={"title": title})


async def batch_delete(client: httpx.AsyncClient, asset_ids: list[str]) -> httpx.Response:
    return await client.post("/api/assets/batch-delete", json={"asset_ids": asset_ids})


async def create_mask(client: httpx.AsyncClient, session: dict) -> str:
    """在会话上点选一次，产生 MASK 素材。"""
    response = await client.post(
        f"/api/sessions/{session['id']}/selection",
        json={"revision": session["revision"], "points": [{"x": 0.5, "y": 0.5}]},
    )
    assert response.status_code == 200, response.text
    return response.json()["mask"]["id"]


async def test_asset_detail_defaults_to_null_title(signed_in: httpx.AsyncClient):
    """LIB-FR-006：AssetOut 增加 title 字段，旧素材默认 null（既有消费方不破坏）。"""
    asset_id = await upload_asset(signed_in)

    detail = (await signed_in.get(f"/api/assets/{asset_id}")).json()

    assert detail["title"] is None


async def test_rename_asset_persists_title(signed_in: httpx.AsyncClient):
    """LIB-FR-006 / LIB-AC-006：重命名返回带新 title 的 AssetOut 并持久化。"""
    asset_id = await upload_asset(signed_in)

    renamed = await rename(signed_in, asset_id, "主图-终稿")

    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "主图-终稿"
    assert renamed.json()["id"] == asset_id
    assert (await signed_in.get(f"/api/assets/{asset_id}")).json()["title"] == "主图-终稿"


async def test_rename_asset_null_and_blank_clear_title(signed_in: httpx.AsyncClient):
    """LIB-FR-006 / LIB-AC-023：null 合法；空白 trim 后归一为 null（UI 回退元信息）。"""
    asset_id = await upload_asset(signed_in)
    assert (await rename(signed_in, asset_id, "旧名字")).status_code == 200

    cleared = await rename(signed_in, asset_id, None)
    assert cleared.status_code == 200
    assert cleared.json()["title"] is None

    blanked = await rename(signed_in, asset_id, "   ")
    assert blanked.status_code == 200
    assert blanked.json()["title"] is None


@pytest.mark.parametrize("title", ["汉" * 80, "😀" * 80])
async def test_rename_asset_accepts_80_codepoints(signed_in: httpx.AsyncClient, title: str):
    """LIB-FR-006 / LIB-AC-023：80 个 Unicode 码点合法（按码点而非 UTF-16 单元计数）。"""
    asset_id = await upload_asset(signed_in)

    renamed = await rename(signed_in, asset_id, title)

    assert renamed.status_code == 200
    assert renamed.json()["title"] == title


@pytest.mark.parametrize("title", ["汉" * 81, "😀" * 81])
async def test_rename_asset_rejects_over_80_codepoints(signed_in: httpx.AsyncClient, title: str):
    """LIB-FR-006 / LIB-AC-023：超过 80 个码点返回 422，不截断。"""
    asset_id = await upload_asset(signed_in)

    renamed = await rename(signed_in, asset_id, title)

    assert renamed.status_code == 422
    assert (await signed_in.get(f"/api/assets/{asset_id}")).json()["title"] is None


@pytest.mark.parametrize("payload", [{}, {"title": 3}, {"title": ["x"]}, {"title": True}])
async def test_rename_asset_rejects_invalid_payloads(
    signed_in: httpx.AsyncClient, payload: dict
):
    """LIB-FR-006：字段缺失或非字符串非 null 返回 422。"""
    asset_id = await upload_asset(signed_in)

    renamed = await signed_in.patch(f"/api/assets/{asset_id}", json=payload)

    assert renamed.status_code == 422


async def test_delete_loose_asset_removes_record_and_object(signed_in: httpx.AsyncClient):
    """LIB-FR-007 / LIB-AC-017 部分：无引用素材删除 204，记录与 S3 对象一起消失。"""
    asset_id = await upload_asset(signed_in)
    key = await storage_key_of(asset_id)

    response = await signed_in.delete(f"/api/assets/{asset_id}")

    assert response.status_code == 204
    assert (await signed_in.get(f"/api/assets/{asset_id}")).status_code == 404
    with pytest.raises(ClientError):
        await storage.get(key)


async def test_delete_asset_referenced_as_current_conflicts(signed_in: httpx.AsyncClient):
    """LIB-FR-007 / LIB-AC-005：当前图被占用返回 409 asset_in_use，列出本人会话与引用类型。"""
    session = await open_session(signed_in, title="在编辑")
    current = session["current_asset_id"]

    response = await signed_in.delete(f"/api/assets/{current}")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "asset_in_use"
    assert "会话" in detail["message"]
    [reference] = detail["references"]
    assert reference["session_id"] == session["id"]
    assert reference["title"] == "在编辑"
    assert "current" in reference["types"]
    # DB 不变：不破坏历史换取删除成功
    assert (await signed_in.get(f"/api/assets/{current}")).status_code == 200


async def test_delete_asset_referenced_as_original_conflicts(signed_in: httpx.AsyncClient):
    """LIB-FR-013：原图引用同样受保护（切换当前图后原图仍被 original 引用）。"""
    session = await open_session(signed_in)
    original = session["current_asset_id"]
    successor = await upload_asset(signed_in)
    switched = await signed_in.patch(
        f"/api/sessions/{session['id']}", json={"current_asset_id": successor}
    )
    assert switched.status_code == 200

    response = await signed_in.delete(f"/api/assets/{original}")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "asset_in_use"
    [reference] = detail["references"]
    assert reference["session_id"] == session["id"]
    assert "original" in reference["types"]
    assert (await signed_in.get(f"/api/assets/{original}")).status_code == 200


async def test_delete_wall_only_asset_cascades_wall_rows(signed_in: httpx.AsyncClient):
    """LIB-FR-013 / LIB-AC-012：仅存在于图片墙不构成保护，删除并级联移除所有墙关联。"""
    extra = await upload_asset(signed_in)
    session = await open_session(signed_in, asset_ids=[extra])

    response = await signed_in.delete(f"/api/assets/{extra}")

    assert response.status_code == 204
    detail = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    assert extra not in [asset["id"] for asset in detail["assets"]]


async def test_delete_mask_asset_returns_unsupported_kind(signed_in: httpx.AsyncClient):
    """LIB-FR-007（§7）/ LIB-AC-022：隐藏派生 kind 单删被拒，返回 unsupported_asset_kind。"""
    session = await open_session(signed_in)
    mask_id = await create_mask(signed_in, session)

    response = await signed_in.delete(f"/api/assets/{mask_id}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "unsupported_asset_kind"
    assert (await signed_in.get(f"/api/assets/{mask_id}")).status_code == 200


async def test_new_asset_endpoints_require_authentication(client: httpx.AsyncClient):
    """LIB-FR-011：重命名 / 单删 / 批删均要求登录。"""
    asset_id = uuid.uuid4()
    assert (await client.patch(f"/api/assets/{asset_id}", json={"title": "x"})).status_code == 401
    assert (await client.delete(f"/api/assets/{asset_id}")).status_code == 401
    assert (
        await client.post("/api/assets/batch-delete", json={"asset_ids": [str(asset_id)]})
    ).status_code == 401


async def test_asset_endpoints_isolated_per_user(
    client: httpx.AsyncClient, credentials, other_credentials
):
    """LIB-FR-011 / LIB-AC-008：非本人与不存在的素材对外表现相同（统一 404 结构）。"""
    await client.post("/api/auth/register", json=credentials)
    asset_id = await upload_asset(client)
    await client.post("/api/auth/logout")
    await client.post("/api/auth/register", json=other_credentials)

    not_found = {"detail": {"code": "not_found", "message": "资源不存在"}}
    for missing in [asset_id, str(uuid.uuid4())]:
        patched = await client.patch(f"/api/assets/{missing}", json={"title": "x"})
        deleted = await client.delete(f"/api/assets/{missing}")
        assert patched.status_code == deleted.status_code == 404
        assert patched.json() == deleted.json() == not_found

    await client.post("/api/auth/login", json=credentials)
    assert (await client.get(f"/api/assets/{asset_id}")).status_code == 200


async def test_batch_delete_reports_deleted_and_skipped(signed_in: httpx.AsyncClient):
    """LIB-FR-008 / LIB-AC-007：三选一被引用 → 已删 2 / 跳过 1，顺序与输入一致。"""
    first = await upload_asset(signed_in)
    referenced = (await open_session(signed_in))["current_asset_id"]
    third = await upload_asset(signed_in)

    response = await batch_delete(signed_in, [first, referenced, third])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] == [first, third]
    [skipped] = body["skipped"]
    assert skipped["id"] == referenced
    assert skipped["code"] == "asset_in_use"
    assert isinstance(skipped["reason"], str) and skipped["reason"]
    assert (await signed_in.get(f"/api/assets/{first}")).status_code == 404
    assert (await signed_in.get(f"/api/assets/{third}")).status_code == 404
    assert (await signed_in.get(f"/api/assets/{referenced}")).status_code == 200


async def test_batch_delete_deduplicates_ids(signed_in: httpx.AsyncClient):
    """LIB-AC-016：按首次出现顺序去重，每个 ID 在响应中最多出现一次。"""
    first = await upload_asset(signed_in)
    second = await upload_asset(signed_in)

    response = await batch_delete(signed_in, [first, second, first])

    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] == [first, second]
    assert body["skipped"] == []


async def test_batch_delete_validates_request_shape(signed_in: httpx.AsyncClient):
    """LIB-AC-016：空数组、超量（>100）、非法 UUID 整批 422 零写入；100 项为合法上限。"""
    asset_id = await upload_asset(signed_in)

    empty = await batch_delete(signed_in, [])
    assert empty.status_code == 422

    over = await batch_delete(signed_in, [asset_id] * 101)
    assert over.status_code == 422
    assert (await signed_in.get(f"/api/assets/{asset_id}")).status_code == 200

    invalid = await batch_delete(signed_in, [asset_id, "not-a-uuid"])
    assert invalid.status_code == 422
    assert (await signed_in.get(f"/api/assets/{asset_id}")).status_code == 200

    boundary = await batch_delete(signed_in, [asset_id] * 100)
    assert boundary.status_code == 200, boundary.text
    assert boundary.json()["deleted"] == [asset_id]
    assert boundary.json()["skipped"] == []


async def test_batch_delete_mixed_ownership_fails_whole_batch(
    client: httpx.AsyncClient, credentials, other_credentials
):
    """LIB-FR-011 / LIB-AC-008：混入非本人/不存在 ID 整批 404、零写入、不泄漏具体 ID。"""
    await client.post("/api/auth/register", json=credentials)
    own = await upload_asset(client)
    await client.post("/api/auth/logout")
    await client.post("/api/auth/register", json=other_credentials)
    foreign = await upload_asset(client)
    await client.post("/api/auth/logout")
    await client.post("/api/auth/login", json=credentials)

    missing = str(uuid.uuid4())
    response = await batch_delete(client, [own, foreign, missing])

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "not_found"
    assert detail["message"] == "资源不存在"
    # 不回显具体越权或缺失 ID
    for leaked in [own, foreign, missing]:
        assert leaked not in response.text
    # 本人合法项也未删除（零写入）
    assert (await client.get(f"/api/assets/{own}")).status_code == 200


async def test_batch_delete_skips_unsupported_kind(signed_in: httpx.AsyncClient):
    """LIB-FR-008 / LIB-AC-022：批删中的 MASK 跳过并标 unsupported_asset_kind，不阻塞其他项。"""
    loose = await upload_asset(signed_in)
    session = await open_session(signed_in)
    mask_id = await create_mask(signed_in, session)

    response = await batch_delete(signed_in, [loose, mask_id])

    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] == [loose]
    [skipped] = body["skipped"]
    assert skipped["id"] == mask_id
    assert skipped["code"] == "unsupported_asset_kind"
    assert (await signed_in.get(f"/api/assets/{mask_id}")).status_code == 200


async def test_s3_failure_after_delete_still_succeeds_with_warning(
    signed_in: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """LIB-FR-017 / LIB-AC-017：DB 提交后 S3 删除失败仍业务成功，warning 含 storage_key 可定位。"""

    async def broken_delete(key: str) -> None:
        raise RuntimeError("s3 down")

    monkeypatch.setattr("app.storage.delete", broken_delete)
    asset_id = await upload_asset(signed_in)
    key = await storage_key_of(asset_id)

    with caplog.at_level(logging.WARNING):
        response = await signed_in.delete(f"/api/assets/{asset_id}")

    assert response.status_code == 204
    assert (await signed_in.get(f"/api/assets/{asset_id}")).status_code == 404
    assert any(
        key in record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING
    )


async def test_batch_delete_keeps_committed_items_when_s3_fails(
    signed_in: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """LIB-FR-017 / LIB-AC-017：批删中 S3 失败不把已提交项改为 skipped。"""

    async def broken_delete(key: str) -> None:
        raise RuntimeError("s3 down")

    monkeypatch.setattr("app.storage.delete", broken_delete)
    first = await upload_asset(signed_in)
    second = await upload_asset(signed_in)

    response = await batch_delete(signed_in, [first, second])

    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] == [first, second]
    assert body["skipped"] == []
    assert (await signed_in.get(f"/api/assets/{first}")).status_code == 404


# ---- 创意库 LIB：素材库全量归属与稳定排序（LIB-FR-002 / §7）----


async def test_library_leftover_excludes_assets_of_sessions_beyond_limit(
    signed_in: httpx.AsyncClient,
):
    """LIB-FR-002 / LIB-AC-018：leftover 先全量排除本人会话墙/引用，再截断 limit；
    超过 limit 的旧会话素材不得误归入「未归入会话」。"""
    oldest_asset = None
    for _ in range(51):
        asset_id = await upload_asset(signed_in)
        response = await signed_in.post("/api/sessions", json={"current_asset_id": asset_id})
        assert response.status_code == 201, response.text
        oldest_asset = oldest_asset or asset_id
    loose = await upload_asset(signed_in)

    groups = (await signed_in.get("/api/assets/library")).json()

    # 只返回最近 50 个会话分组
    session_groups = [group for group in groups if group["session_id"] is not None]
    assert len(session_groups) == 50
    # 最旧会话（超出 limit）的墙素材不出现在任何分组，包括 leftover 与封面
    everywhere = {
        asset["id"] for group in groups for asset in [group["cover"], *group["assets"]]
    }
    assert oldest_asset not in everywhere
    # leftover 只含真正的未归组素材
    leftover = groups[-1]
    assert leftover["session_id"] is None
    assert [asset["id"] for asset in leftover["assets"]] == [loose]


async def test_library_leftover_truncates_to_limit(signed_in: httpx.AsyncClient):
    """LIB-AC-018：未归组素材在排除归属后按 created_at 倒序截断 limit。"""
    ids = [await upload_asset(signed_in) for _ in range(51)]

    groups = (await signed_in.get("/api/assets/library")).json()

    leftover = groups[-1]
    assert leftover["session_id"] is None
    visible = [asset["id"] for asset in leftover["assets"]]
    assert len(visible) == 50
    assert visible == list(reversed(ids[1:]))


async def test_library_session_groups_tie_break_by_id_ascending(signed_in: httpx.AsyncClient):
    """LIB-FR-002（§7 稳定排序）：library 会话分组 updated_at 相同时按 id 升序。"""
    ids = [(await open_session(signed_in))["id"] for _ in range(3)]
    async with SessionFactory() as session:
        await session.execute(
            text("UPDATE edit_sessions SET updated_at = now() WHERE id = ANY(:ids)"),
            {"ids": [str(item) for item in ids]},
        )
        await session.commit()

    groups = (await signed_in.get("/api/assets/library")).json()

    assert [group["session_id"] for group in groups] == sorted(ids, key=uuid.UUID)


# ---- 创意库会话组展示最新画布快照（与编辑器图片墙计数对齐）----


async def attach_to_wall(session_id: str, asset_id: str, position: int) -> None:
    """把已有素材按 position 挂到会话墙。"""
    async with SessionFactory() as db:
        db.add(
            SessionAsset(
                session_id=uuid.UUID(session_id),
                asset_id=uuid.UUID(asset_id),
                position=position,
            )
        )
        await db.commit()


async def seed_wall_asset(
    client: httpx.AsyncClient,
    session_id: str,
    kind: str,
    position: int,
    storage_suffix: str,
) -> str:
    """DB 直插指定 kind 的墙素材并按 position 挂到会话墙上（不走真实工具链路）。"""
    user_id = (await client.get("/api/auth/me")).json()["id"]
    async with SessionFactory() as db:
        asset = Asset(
            user_id=uuid.UUID(user_id),
            kind=kind,
            source="tool",
            storage_key=f"test/{storage_suffix}/{uuid.uuid4()}",
            image_format="PNG",
            width=64,
            height=64,
            size_bytes=128,
            has_alpha=False,
        )
        db.add(asset)
        await db.flush()
        db.add(
            SessionAsset(
                session_id=uuid.UUID(session_id),
                asset_id=asset.id,
                position=position,
            )
        )
        await db.commit()
        return str(asset.id)


async def test_library_includes_latest_snapshot_for_session(signed_in: httpx.AsyncClient):
    """会话墙含多张快照时，会话组仅展示 wall 末位最新一张（与编辑器图片墙一致）。"""
    session = await open_session(signed_in, title="快照会话")
    generated = await upload_asset(signed_in)
    await attach_to_wall(session["id"], generated, 1)

    old_snapshot = await seed_wall_asset(signed_in, session["id"], "snapshot", 2, "old")
    await seed_wall_asset(signed_in, session["id"], "mask", 3, "mask")
    await seed_wall_asset(signed_in, session["id"], "subject", 4, "subject")
    latest_snapshot = await seed_wall_asset(signed_in, session["id"], "snapshot", 5, "new")

    groups = (await signed_in.get("/api/assets/library")).json()
    group = next(item for item in groups if item["session_id"] == session["id"])

    ids = [asset["id"] for asset in group["assets"]]
    kinds = {asset["id"]: asset["kind"] for asset in group["assets"]}
    assert session["current_asset_id"] in ids
    assert generated in ids
    assert latest_snapshot in ids and kinds[latest_snapshot] == "snapshot"
    assert old_snapshot not in ids
    assert all(kind not in {"mask", "subject", "background"} for kind in kinds.values())


async def test_library_keeps_latest_snapshot_when_current_is_original(signed_in: httpx.AsyncClient):
    """当前作品是原图（非快照）时，会话组仍保留最新快照入口（编辑器墙同语义）。"""
    session = await open_session(signed_in, title="切回原图")
    await seed_wall_asset(signed_in, session["id"], "snapshot", 1, "only")

    groups = (await signed_in.get("/api/assets/library")).json()
    group = next(item for item in groups if item["session_id"] == session["id"])

    ids = [asset["id"] for asset in group["assets"]]
    assert session["current_asset_id"] in ids
    assert any(asset["kind"] == "snapshot" for asset in group["assets"])


async def test_library_leftover_never_leaks_snapshot(signed_in: httpx.AsyncClient):
    """孤儿快照不属于任何会话墙，不得泄漏进「未归入会话」分组。"""
    await upload_asset(signed_in)  # 普通孤儿素材，保证 leftover 组存在
    user_id = (await signed_in.get("/api/auth/me")).json()["id"]
    async with SessionFactory() as db:
        orphan = Asset(
            user_id=uuid.UUID(user_id),
            kind="snapshot",
            source="tool",
            storage_key=f"test/orphan/{uuid.uuid4()}",
            image_format="PNG",
            width=64,
            height=64,
            size_bytes=128,
            has_alpha=False,
        )
        db.add(orphan)
        await db.commit()

    groups = (await signed_in.get("/api/assets/library")).json()
    leftover = groups[-1]

    assert leftover["session_id"] is None
    assert all(asset["kind"] != "snapshot" for asset in leftover["assets"])
    assert str(orphan.id) not in [asset["id"] for asset in leftover["assets"]]
