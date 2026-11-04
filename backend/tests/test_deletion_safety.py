"""创意库 LIB：R(X) 全量引用扫描与删除安全（LIB-FR-013 / LIB-AC-004、005、012、017、022、024）。

覆盖 PRD §7：
- 受保护引用集合 R(X) 覆盖 original/current、当前 document 全部图层（含隐藏图层）、
  保留历史（params.before 与 result 的 current_asset_id 与图层 asset_id），含重做分支；
- 2026-10-23 修订（WI-20261023-02）：仅 history 引用不再阻断删除，转隐藏式删除
  （LIB-FR-007/018）+ 延迟物理回收（LIB-FR-019）；original/current/layer 仍 409，
  references 只列阻断会话与阻断类型；
- 损坏引用保守阻止删除（reference_state_invalid），不当作“无引用”；
- 不递归把任意 JSON 字符串猜作 asset ID；
- 删除事务失败整体回滚且不调用 S3；S3 失败不回滚业务删除；
- 迁移 upgrade → downgrade → upgrade（LIB-AC-024、028）。
"""

import logging
import uuid

import httpx
import pytest
from botocore.exceptions import ClientError
from sqlalchemy import func, inspect, select, text

from app import storage
from app.db import SessionFactory, engine
from app.models import Asset, EditHistory, EditSession, SessionAsset, User
from tests.canvas import apply, layers, scene
from tests.test_sessions import (
    confirm_delete,
    deletion_preview,
    open_session,
    storage_key_of,
    upload,
)


@pytest.fixture
async def signed_in(client: httpx.AsyncClient, credentials):
    await client.post("/api/auth/register", json=credentials)
    return client


async def _user_id(client: httpx.AsyncClient, credentials: dict) -> uuid.UUID:
    """从注册用户名反查 user_id，供 DB 直写场景使用。"""
    del client
    async with SessionFactory() as session:
        return await session.scalar(select(User.id).where(User.username == credentials["username"]))


async def _history_count(session_id: uuid.UUID) -> int:
    async with SessionFactory() as session:
        return await session.scalar(
            select(func.count())
            .select_from(EditHistory)
            .where(EditHistory.session_id == session_id)
        )


async def _asset_count(user_id: uuid.UUID) -> int:
    async with SessionFactory() as session:
        return await session.scalar(
            select(func.count()).select_from(Asset).where(Asset.user_id == user_id)
        )


async def _asset_row(asset_id: str) -> Asset | None:
    async with SessionFactory() as session:
        return await session.get(Asset, uuid.UUID(asset_id))


async def _wall_count(asset_id: str) -> int:
    async with SessionFactory() as session:
        return await session.scalar(
            select(func.count())
            .select_from(SessionAsset)
            .where(SessionAsset.asset_id == uuid.UUID(asset_id))
        )


async def test_delete_history_only_asset_hides_and_undo_restores(
    signed_in: httpx.AsyncClient,
):
    """LIB-FR-007/018 / LIB-AC-012/025（2026-10-23 修订）：A→B→C 中 B 仅被保留历史引用
    → 隐藏式删除（204）：行与对象保留、墙关联清空、创意库与编辑器图片墙不可见；
    撤销恢复 B 为当前图时详情必须带出 B（活跃并集），重做恢复正常。"""
    session = await open_session(signed_in)
    middle = (await apply(signed_in, session["id"], "adjust_image", {"brightness": 0.1}))[
        "current_asset_id"
    ]
    latest = (await apply(signed_in, session["id"], "adjust_image", {"brightness": 0.2}))[
        "current_asset_id"
    ]
    assert middle != session["original_asset_id"] != latest
    middle_key = await storage_key_of(middle)

    response = await signed_in.delete(f"/api/assets/{middle}")

    assert response.status_code == 204
    row = await _asset_row(middle)
    assert row is not None and row.library_hidden is True
    assert await _wall_count(middle) == 0
    assert await storage.get(middle_key)
    # 管理入口 404；创意库分组与编辑器图片墙均不再出现
    assert (await signed_in.get(f"/api/assets/{middle}")).status_code == 404
    library = (await signed_in.get("/api/assets/library")).json()
    assert middle not in [asset["id"] for group in library for asset in group["assets"]]
    detail = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    assert middle not in [asset["id"] for asset in detail["assets"]]

    # 撤销把 B 恢复为当前图：画布渲染依赖的素材随详情带出，撤销/重做链完好
    undone = (await signed_in.post(f"/api/sessions/{session['id']}/undo")).json()
    assert undone["current_asset_id"] == middle
    assert middle in [asset["id"] for asset in undone["assets"]]
    # 当前作品锚点同样随创意库会话组展示，与编辑器图片墙计数对齐（LIB-FR-018）
    library_undone = (await signed_in.get("/api/assets/library")).json()
    [group] = [g for g in library_undone if g["session_id"] == session["id"]]
    assert middle in [asset["id"] for asset in group["assets"]]
    redone = (await signed_in.post(f"/api/sessions/{session['id']}/redo")).json()
    assert redone["current_asset_id"] == latest
    assert middle not in [asset["id"] for asset in redone["assets"]]
    library_redone = (await signed_in.get("/api/assets/library")).json()
    [group] = [g for g in library_redone if g["session_id"] == session["id"]]
    assert middle not in [asset["id"] for asset in group["assets"]]


async def test_delete_redo_branch_asset_hides_and_redo_survives(signed_in: httpx.AsyncClient):
    """LIB-FR-007/018 / LIB-AC-012（2026-10-23 修订）：撤销后重做分支里的历史引用
    同样不再阻断删除；转隐藏后重做仍可用，恢复的素材随活跃状态重新可见。"""
    session = await open_session(signed_in)
    successor = (await apply(signed_in, session["id"], "adjust_image", {"brightness": 0.1}))[
        "current_asset_id"
    ]
    undone = await signed_in.post(f"/api/sessions/{session['id']}/undo")
    assert undone.status_code == 200

    response = await signed_in.delete(f"/api/assets/{successor}")

    assert response.status_code == 204
    row = await _asset_row(successor)
    assert row is not None and row.library_hidden is True
    # 隐藏不破坏重做：重做把 successor 恢复为当前图并随详情带出
    redone = (await signed_in.post(f"/api/sessions/{session['id']}/redo")).json()
    assert redone["current_asset_id"] == successor
    assert successor in [asset["id"] for asset in redone["assets"]]


async def test_delete_blocked_lists_blocking_references_only(
    signed_in: httpx.AsyncClient, credentials
):
    """LIB-FR-007/011 / LIB-AC-027（2026-10-23 修订）：Y 的 current 阻断删除时，
    409 references 仅列阻断会话与阻断类型；Z 的 history 引用不进入列表。"""
    shared = await upload(signed_in)
    holder = await open_session(signed_in, current_asset_id=shared, title="Y")
    other = await open_session(signed_in, title="Z")
    owner = await _user_id(signed_in, credentials)
    async with SessionFactory() as db:
        db.add(
            EditHistory(
                user_id=owner,
                session_id=uuid.UUID(other["id"]),
                seq=2,
                action="note",
                params={"before": {"current_asset_id": shared, "revision": 1}},
                result={"current_asset_id": other["current_asset_id"], "revision": 2},
            )
        )
        await db.commit()

    response = await signed_in.delete(f"/api/assets/{shared}")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "asset_in_use"
    [reference] = detail["references"]
    assert reference["session_id"] == holder["id"]
    assert "history" not in reference["types"]
    assert "current" in reference["types"]


async def test_hidden_asset_addressability(signed_in: httpx.AsyncClient):
    """LIB-FR-011/018 / LIB-AC-028：已隐藏素材的单查/重命名/单删/建会话按 404，
    批量删除按 not_found 跳过，不把隐藏行暴露为可管理资源。"""
    session = await open_session(signed_in)
    middle = (await apply(signed_in, session["id"], "adjust_image", {"brightness": 0.1}))[
        "current_asset_id"
    ]
    # 第二次编辑让 middle 退为仅历史引用（否则仍为当前图，属阻断引用）
    await apply(signed_in, session["id"], "adjust_image", {"brightness": 0.2})
    assert (await signed_in.delete(f"/api/assets/{middle}")).status_code == 204

    assert (await signed_in.get(f"/api/assets/{middle}")).status_code == 404
    patch = await signed_in.patch(f"/api/assets/{middle}", json={"title": "复活"})
    assert patch.status_code == 404
    assert (await signed_in.delete(f"/api/assets/{middle}")).status_code == 404
    create = await signed_in.post("/api/sessions", json={"current_asset_id": middle})
    assert create.status_code == 404

    fresh = await upload(signed_in)
    batch = await signed_in.post(
        "/api/assets/batch-delete", json={"asset_ids": [middle, fresh]}
    )

    assert batch.status_code == 200
    body = batch.json()
    assert body["deleted"] == [fresh]
    assert [(item["id"], item["code"]) for item in body["skipped"]] == [(middle, "not_found")]


async def test_sweep_reclaims_unreferenced_hidden_asset(signed_in: httpx.AsyncClient):
    """LIB-FR-019 / LIB-AC-026：引用消失前隐藏素材不回收、不回流未归组；
    引用消失（历史滚出保留窗）后被下一次删除类操作顺带物理回收。"""
    session = await open_session(signed_in)
    middle = (await apply(signed_in, session["id"], "adjust_image", {"brightness": 0.1}))[
        "current_asset_id"
    ]
    # 第二次编辑让 middle 退为仅历史引用
    await apply(signed_in, session["id"], "adjust_image", {"brightness": 0.2})
    middle_key = await storage_key_of(middle)
    first = await upload(signed_in)
    second = await upload(signed_in)
    assert (await signed_in.delete(f"/api/assets/{middle}")).status_code == 204

    # 引用仍在：触发其他删除不回收
    assert (await signed_in.delete(f"/api/assets/{first}")).status_code == 204
    row = await _asset_row(middle)
    assert row is not None and row.library_hidden is True

    # 模拟历史滚出保留窗：清空该会话历史后 middle 无任何 R(X) 引用
    async with SessionFactory() as db:
        await db.execute(
            text("DELETE FROM edit_history WHERE session_id = :sid"),
            {"sid": session["id"]},
        )
        await db.commit()

    library = (await signed_in.get("/api/assets/library")).json()
    assert middle not in [asset["id"] for group in library for asset in group["assets"]]

    assert (await signed_in.delete(f"/api/assets/{second}")).status_code == 204
    assert await _asset_row(middle) is None
    with pytest.raises(ClientError):
        await storage.get(middle_key)


async def test_session_delete_reclaims_hidden_exclusive_asset(signed_in: httpx.AsyncClient):
    """LIB-FR-019 / LIB-AC-026：会话删除按既有 C(X)/D(X) 连带物理回收独占的已隐藏素材。"""
    session = await open_session(signed_in)
    middle = (await apply(signed_in, session["id"], "adjust_image", {"brightness": 0.1}))[
        "current_asset_id"
    ]
    # 第二次编辑让 middle 退为仅历史引用
    await apply(signed_in, session["id"], "adjust_image", {"brightness": 0.2})
    middle_key = await storage_key_of(middle)
    assert (await signed_in.delete(f"/api/assets/{middle}")).status_code == 204

    token = (await deletion_preview(signed_in, session["id"]))["preview_token"]
    response = await confirm_delete(signed_in, session["id"], token)

    assert response.status_code == 204
    assert await _asset_row(middle) is None
    with pytest.raises(ClientError):
        await storage.get(middle_key)


async def test_delete_asset_of_hidden_layer_conflicts(signed_in: httpx.AsyncClient):
    """LIB-FR-013 / LIB-AC-012：当前 document 的隐藏图层引用同样计入 R(X)，types 含 layer。

    用 base 图层（可管理 kind）承载隐藏语义：subject/background 属隐藏派生 kind，
    按 PRD §7 单独走 unsupported_asset_kind（见 test_assets 的 mask 用例）。
    """
    session = await open_session(signed_in, image=scene())
    hidden = await apply(
        signed_in,
        session["id"],
        "set_layer_visible",
        {"layer_id": "base", "visible": False},
    )
    base_asset = layers(hidden)["base"]["asset_id"]
    assert base_asset

    response = await signed_in.delete(f"/api/assets/{base_asset}")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "asset_in_use"
    assert "layer" in detail["references"][0]["types"]
    assert (await signed_in.get(f"/api/assets/{base_asset}")).status_code == 200


async def test_shared_asset_survives_session_delete_and_other_session_history_works(
    signed_in: httpx.AsyncClient,
):
    """LIB-FR-013 / LIB-AC-004：被其他会话历史引用的共用素材在会话删除后保留，Y 撤销/重做正常。"""
    shared = await upload(signed_in)
    other = await upload(signed_in)
    session_x = await open_session(signed_in, current_asset_id=other, asset_ids=[shared], title="X")
    session_y = await open_session(signed_in, current_asset_id=shared, title="Y")
    newest = (await apply(signed_in, session_y["id"], "adjust_image", {"brightness": 0.1}))[
        "current_asset_id"
    ]
    shared_key = await storage_key_of(shared)

    token = (await deletion_preview(signed_in, session_x["id"]))["preview_token"]
    response = await confirm_delete(signed_in, session_x["id"], token)

    assert response.status_code == 204
    assert (await signed_in.get(f"/api/assets/{shared}")).status_code == 200
    assert await storage.get(shared_key)
    # Y 的编辑历史恢复正常：撤销回到 shared，重做回到 newest
    undone = (await signed_in.post(f"/api/sessions/{session_y['id']}/undo")).json()
    assert undone["current_asset_id"] == shared
    redone = (await signed_in.post(f"/api/sessions/{session_y['id']}/redo")).json()
    assert redone["current_asset_id"] == newest


async def test_corrupt_document_blocks_session_deletion(signed_in: httpx.AsyncClient):
    """LIB-FR-013（§7）/ LIB-AC-022：无法解析的 document 保守阻止删除。"""
    session = await open_session(signed_in)
    session_uuid = uuid.UUID(session["id"])
    async with SessionFactory() as db:
        await db.execute(
            text("UPDATE edit_sessions SET document = :doc WHERE id = :sid"),
            {"doc": '{"width": "not-a-number", "layers": []}', "sid": session["id"]},
        )
        await db.commit()

    preview = await signed_in.get(f"/api/sessions/{session['id']}/deletion-preview")

    assert preview.status_code == 409
    assert preview.json()["detail"]["code"] == "reference_state_invalid"
    # 会话保持原样（DB 行仍在），不允许绕过删除
    async with SessionFactory() as db:
        alive = await db.scalar(select(EditSession.id).where(EditSession.id == session_uuid))
    assert alive is not None


async def test_corrupt_history_blocks_asset_deletion(signed_in: httpx.AsyncClient):
    """LIB-FR-013（§7）：任一会话的保留历史无法安全解析时，素材删除保守阻止。"""
    session = await open_session(signed_in)
    loose = await upload(signed_in)
    async with SessionFactory() as db:
        await db.execute(
            text("UPDATE edit_history SET params = :params WHERE session_id = :sid"),
            {"params": '{"before": 42}', "sid": session["id"]},
        )
        await db.commit()

    response = await signed_in.delete(f"/api/assets/{loose}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "reference_state_invalid"
    assert (await signed_in.get(f"/api/assets/{loose}")).status_code == 200


async def test_unrelated_uuid_strings_do_not_block_deletion(signed_in: httpx.AsyncClient):
    """LIB-FR-013（§7）：不递归把任意 JSON 字符串猜作 asset ID，无关字符串不构成引用。"""
    session = await open_session(signed_in)
    loose = await upload(signed_in)
    async with SessionFactory() as db:
        owner = await db.scalar(
            select(EditSession.user_id).where(EditSession.id == uuid.UUID(session["id"]))
        )
        db.add(
            EditHistory(
                user_id=owner,
                session_id=uuid.UUID(session["id"]),
                seq=2,
                action="note",
                params={"note": loose},
                result={"ok": True},
            )
        )
        await db.commit()

    response = await signed_in.delete(f"/api/assets/{loose}")

    assert response.status_code == 204
    assert (await signed_in.get(f"/api/assets/{loose}")).status_code == 404


async def test_session_delete_rolls_back_and_skips_s3_on_db_failure(
    signed_in: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, credentials
):
    """LIB-FR-017 / LIB-AC-022：删除事务失败整体回滚（会话与素材保留）且 S3 零调用。

    用一张守表对 D(X) 素材加 RESTRICT 外键，在数据库层注入事务失败，
    不依赖实现内部结构；transport 关闭异常上抛以拿到 500 响应。
    """
    from httpx import ASGITransport

    from app.main import app

    session = await open_session(signed_in)
    protected = session["current_asset_id"]
    calls: list[str] = []
    real_delete = storage.delete

    async def recording_delete(key: str) -> None:
        calls.append(key)
        await real_delete(key)

    monkeypatch.setattr("app.storage.delete", recording_delete)

    guard = f"_lib_guard_{uuid.uuid4().hex[:8]}"
    try:
        async with SessionFactory() as db:
            await db.execute(
                text(
                    f"CREATE TABLE {guard} "
                    "(asset_id uuid NOT NULL REFERENCES assets(id) ON DELETE RESTRICT)"
                )
            )
            await db.execute(text(f"INSERT INTO {guard} (asset_id) VALUES (:a)"), {"a": protected})
            await db.commit()

        token = (await deletion_preview(signed_in, session["id"]))["preview_token"]
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as lenient:
            lenient.cookies.update(signed_in.cookies)
            response = await lenient.delete(
                f"/api/sessions/{session['id']}", headers={"X-Deletion-Preview": token}
            )
    finally:
        async with SessionFactory() as db:
            await db.execute(text(f"DROP TABLE IF EXISTS {guard}"))
            await db.commit()

    assert response.status_code in (409, 500)
    assert calls == []  # 未提交的删除不得调用 storage.delete
    assert (await signed_in.get(f"/api/sessions/{session['id']}")).status_code == 200
    assert (await signed_in.get(f"/api/assets/{protected}")).status_code == 200


async def test_session_delete_s3_failure_warns_but_succeeds(
    signed_in: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """LIB-FR-017 / LIB-AC-017：会话删除 DB 提交后 S3 清理失败仍业务成功，warning 可定位。"""

    async def broken_delete(key: str) -> None:
        raise RuntimeError("s3 down")

    monkeypatch.setattr("app.storage.delete", broken_delete)
    session = await open_session(signed_in)
    current = session["current_asset_id"]
    key = await storage_key_of(current)
    token = (await deletion_preview(signed_in, session["id"]))["preview_token"]

    with caplog.at_level(logging.WARNING):
        response = await confirm_delete(signed_in, session["id"], token)

    assert response.status_code == 204
    assert (await signed_in.get(f"/api/sessions/{session['id']}")).status_code == 404
    assert (await signed_in.get(f"/api/assets/{current}")).status_code == 404
    assert any(
        key in record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING
    )
    # 对象清理失败时对象残留（PRD AC-017 如实记录的孤儿风险），业务删除不回滚
    assert await storage.get(key)


async def _asset_columns() -> set[str]:
    async with engine.connect() as connection:
        return {
            column["name"]
            for column in await connection.run_sync(
                lambda sync: inspect(sync).get_columns("assets")
            )
        }


async def test_asset_title_and_library_hidden_migration_round_trip():
    """LIB-AC-024/028：upgrade → downgrade → upgrade 循环；title 与 library_hidden
    两列随各自迁移增删，其余列不受影响。

    alembic 的 env.py 内部使用 asyncio.run，须放到独立线程执行；
    断言失败（Red 阶段 library_hidden 列尚不存在）发生在任何 downgrade 之前，
    不会破坏现有 schema。
    """
    import asyncio
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))

    await asyncio.to_thread(command.upgrade, config, "head")
    assert {"title", "library_hidden"} <= await _asset_columns()

    try:
        # 以 title/library_hidden 各自迁移的绝对 revision 为锚点降级（原为相对 -1）：
        # 本测试钉的是「两列随各自迁移增删、其余列不受影响」，不是「head 之后的
        # 迁移栈深度」；后续新迁移（如 WI-20261025-01 的 user_admin_fields）入列后
        # 相对锚点不再指向这两列的迁移，断言本体不变。
        await asyncio.to_thread(command.downgrade, config, "3f2a1c9d5e47")
        assert "title" in await _asset_columns()
        assert "library_hidden" not in await _asset_columns()
        await asyncio.to_thread(command.downgrade, config, "17d993e08c71")
        # 降级只移除命名字段，既有列不受影响
        assert "title" not in await _asset_columns()
        assert {"id", "user_id", "kind", "storage_key"} <= await _asset_columns()
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")

    assert {"title", "library_hidden"} <= await _asset_columns()
