"""创意库 LIB：R(X) 全量引用扫描与删除安全（LIB-FR-013 / LIB-AC-004、005、012、017、022、024）。

覆盖 PRD §7：
- 受保护引用集合 R(X) 覆盖 original/current、当前 document 全部图层（含隐藏图层）、
  保留历史（params.before 与 result 的 current_asset_id 与图层 asset_id），含重做分支；
- 损坏引用保守阻止删除（reference_state_invalid），不当作“无引用”；
- 不递归把任意 JSON 字符串猜作 asset ID；
- 删除事务失败整体回滚且不调用 S3；S3 失败不回滚业务删除；
- 迁移 upgrade → downgrade → upgrade（LIB-AC-024）。
"""

import logging
import uuid

import httpx
import pytest
from sqlalchemy import func, inspect, select, text

from app import storage
from app.db import SessionFactory, engine
from app.models import Asset, EditHistory, EditSession, User
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


async def test_delete_asset_in_history_chain_conflicts_and_undo_redo_survive(
    signed_in: httpx.AsyncClient,
):
    """LIB-FR-013 / LIB-AC-012：A→B→C 中 B 仅被保留历史引用 → 409 asset_in_use（history），
    随后撤销恢复 B、重做恢复 C 均正常。"""
    session = await open_session(signed_in)
    middle = (await apply(signed_in, session["id"], "adjust_image", {"brightness": 0.1}))[
        "current_asset_id"
    ]
    latest = (await apply(signed_in, session["id"], "adjust_image", {"brightness": 0.2}))[
        "current_asset_id"
    ]
    assert middle != session["original_asset_id"] != latest

    response = await signed_in.delete(f"/api/assets/{middle}")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "asset_in_use"
    [reference] = detail["references"]
    assert reference["session_id"] == session["id"]
    assert "history" in reference["types"]
    assert (await signed_in.get(f"/api/assets/{middle}")).status_code == 200

    undone = (await signed_in.post(f"/api/sessions/{session['id']}/undo")).json()
    assert undone["current_asset_id"] == middle
    redone = (await signed_in.post(f"/api/sessions/{session['id']}/redo")).json()
    assert redone["current_asset_id"] == latest


async def test_delete_asset_in_redo_branch_conflicts(signed_in: httpx.AsyncClient):
    """LIB-FR-013 / LIB-AC-012：撤销后 redo 分支里的历史引用同样受保护，重做仍可用。"""
    session = await open_session(signed_in)
    successor = (await apply(signed_in, session["id"], "adjust_image", {"brightness": 0.1}))[
        "current_asset_id"
    ]
    undone = await signed_in.post(f"/api/sessions/{session['id']}/undo")
    assert undone.status_code == 200

    response = await signed_in.delete(f"/api/assets/{successor}")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "asset_in_use"
    assert "history" in detail["references"][0]["types"]
    # 拒绝删除不破坏重做能力
    redone = (await signed_in.post(f"/api/sessions/{session['id']}/redo")).json()
    assert redone["current_asset_id"] == successor


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


async def test_asset_title_migration_round_trip():
    """LIB-AC-024：upgrade → downgrade → upgrade 循环；title 列随迁移增删，其余列不受影响。

    alembic 的 env.py 内部使用 asyncio.run，须放到独立线程执行；
    断言失败（Red 阶段 title 列尚不存在）发生在任何 downgrade 之前，不会破坏现有 schema。
    """
    import asyncio
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))

    await asyncio.to_thread(command.upgrade, config, "head")
    assert "title" in await _asset_columns()

    try:
        await asyncio.to_thread(command.downgrade, config, "-1")
        assert "title" not in await _asset_columns()
        # 降级只移除命名字段，既有列不受影响
        assert {"id", "user_id", "kind", "storage_key"} <= await _asset_columns()
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")

    assert "title" in await _asset_columns()
