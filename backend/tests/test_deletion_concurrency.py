"""创意库 LIB：活动任务门禁与并发删除（LIB-FR-014 / PRD §7.1 / LIB-AC-013、015、016）。

覆盖：
- 保守门禁：本人任一 queued/running ToolRun / AgentRun（含待确认计划、无会话生成与批量）
  阻止会话/素材删除；他人任务不阻塞；重命名、查看不受影响；
- WI 决策 1 的用户级事务咨询锁临界区（pg_advisory_xact_lock(920620, hashtext(user_id))）；
- 受控竞争（asyncio.gather，不以 sleep 猜时序）：提交/删除、引用写入/删除只允许合法顺序；
- 迟到 worker 的执行代次校验（WI 决策 2，复用 retries）；
- 批删预检后新出现任务按 §8.2.4 逐项跳过；非预期 DB 异常不回滚已提交项（§8.2.6）。
"""

import asyncio
import dataclasses
import uuid

import httpx
import pytest
from sqlalchemy import func, select, text

import app.tools as tools_registry
from app import storage
from app.db import SessionFactory
from app.models import Asset, EditHistory, ToolRun, User
from app.models.agent_run import AgentRun
from app.models.tool_run import RunStatus
from app.services import runs as runs_service
from app.tasks.tools import run_tool
from app.tools import spec_of
from tests.canvas import invoke
from tests.test_assets import make_image
from tests.test_sessions import confirm_delete, deletion_preview, open_session, upload

# WI 决策 1 固定的用户级事务咨询锁命名空间：
# pg_advisory_xact_lock(920620, hashtext(user_id::text))
LOCK_NAMESPACE = 920620


@pytest.fixture
async def signed_in(client: httpx.AsyncClient, credentials):
    await client.post("/api/auth/register", json=credentials)
    return client


async def _user_id(credentials: dict) -> uuid.UUID:
    """按注册用户名反查 user_id，供 DB 直写场景使用。"""
    async with SessionFactory() as session:
        return await session.scalar(
            select(User.id).where(User.username == credentials["username"])
        )


async def _insert_run(user_id: uuid.UUID, status: RunStatus) -> None:
    """直接落一条 ToolRun，构造 queued/running 活动任务。"""
    async with SessionFactory() as session:
        session.add(
            ToolRun(
                user_id=user_id,
                session_id=None,
                tool="generate_image",
                status=status,
                params={},
                result={},
                progress=0,
                stage="",
                retries=0,
            )
        )
        await session.commit()


async def _start_generation(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        "/api/generations", json={"prompt": "杯子", "ratio": "1:1", "count": 1}
    )


async def _asset_count(user_id: uuid.UUID) -> int:
    async with SessionFactory() as session:
        return await session.scalar(
            select(func.count()).select_from(Asset).where(Asset.user_id == user_id)
        )


async def _history_count(session_id: uuid.UUID) -> int:
    async with SessionFactory() as session:
        return await session.scalar(
            select(func.count())
            .select_from(EditHistory)
            .where(EditHistory.session_id == session_id)
        )


# ---- 保守门禁（LIB-FR-014 / LIB-AC-013）----


async def test_queued_generation_blocks_deletion_until_finished(signed_in: httpx.AsyncClient):
    """LIB-FR-014 / LIB-AC-013：无会话的排队生成阻止本人一切删除；任务终结后可重试。"""
    session = await open_session(signed_in)
    loose = await upload(signed_in)
    token = (await deletion_preview(signed_in, session["id"]))["preview_token"]

    started = await _start_generation(signed_in)
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]

    blocked = await deletion_preview(signed_in, session["id"])
    assert blocked["blocked"] is True
    assert [item["code"] for item in blocked["blockers"]] == ["active_tasks"]

    refused = await confirm_delete(signed_in, session["id"], token)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "active_tasks"

    asset_refused = await signed_in.delete(f"/api/assets/{loose}")
    assert asset_refused.status_code == 409
    assert asset_refused.json()["detail"]["code"] == "active_tasks"

    batch_refused = await signed_in.post("/api/assets/batch-delete", json={"asset_ids": [loose]})
    assert batch_refused.status_code == 409
    assert batch_refused.json()["detail"]["code"] == "active_tasks"
    # 门禁期间零写入
    assert (await signed_in.get(f"/api/sessions/{session['id']}")).status_code == 200
    assert (await signed_in.get(f"/api/assets/{loose}")).status_code == 200

    # 任务真正终结后可重试
    await run_tool({}, uuid.UUID(run_id))
    fresh = await deletion_preview(signed_in, session["id"])
    assert fresh["blocked"] is False
    retried = await confirm_delete(signed_in, session["id"], fresh["preview_token"])
    assert retried.status_code == 204
    assert (await signed_in.get(f"/api/sessions/{session['id']}")).status_code == 404
    # loose 不在 X 的墙/引用里，不随会话删除
    assert (await signed_in.get(f"/api/assets/{loose}")).status_code == 200


async def test_running_tool_run_blocks_deletion(signed_in: httpx.AsyncClient, credentials):
    """LIB-FR-014 / LIB-AC-013：running 状态的 ToolRun 同样阻止删除。"""
    session = await open_session(signed_in)
    await _insert_run(await _user_id(credentials), RunStatus.RUNNING)

    blocked = await deletion_preview(signed_in, session["id"])
    assert blocked["blocked"] is True
    assert blocked["blockers"][0]["code"] == "active_tasks"

    refused = await confirm_delete(signed_in, session["id"], blocked["preview_token"])
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "active_tasks"
    assert (await signed_in.get(f"/api/sessions/{session['id']}")).status_code == 200


async def test_queued_agent_plan_blocks_deletion(signed_in: httpx.AsyncClient, credentials):
    """LIB-FR-014 / LIB-AC-013：待确认计划（AgentRun QUEUED）同样阻止本人删除。"""
    session = await open_session(signed_in)
    async with SessionFactory() as db:
        db.add(
            AgentRun(
                user_id=await _user_id(credentials),
                session_id=uuid.UUID(session["id"]),
                revision=session["revision"],
                goal="待确认计划",
                plan=[],
                status=RunStatus.QUEUED,
            )
        )
        await db.commit()

    blocked = await deletion_preview(signed_in, session["id"])
    assert blocked["blocked"] is True
    assert blocked["blockers"][0]["code"] == "active_tasks"

    refused = await confirm_delete(signed_in, session["id"], blocked["preview_token"])
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "active_tasks"
    assert (await signed_in.get(f"/api/sessions/{session['id']}")).status_code == 200


async def test_queued_batch_blocks_deletion(signed_in: httpx.AsyncClient):
    """LIB-FR-014 / LIB-AC-013：无会话批量任务同样阻止删除。"""
    session = await open_session(signed_in)
    loose = await upload(signed_in)
    started = await signed_in.post(
        "/api/batches",
        json={"asset_ids": [loose], "operations": [{"tool": "remove_background", "params": {}}]},
    )
    assert started.status_code == 202, started.text

    blocked = await deletion_preview(signed_in, session["id"])
    assert blocked["blocked"] is True

    refused = await confirm_delete(signed_in, session["id"], blocked["preview_token"])
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "active_tasks"
    asset_refused = await signed_in.delete(f"/api/assets/{loose}")
    assert asset_refused.status_code == 409
    assert (await signed_in.get(f"/api/assets/{loose}")).status_code == 200


async def test_other_users_active_tasks_do_not_block(
    client: httpx.AsyncClient, credentials, other_credentials
):
    """LIB-FR-014 / LIB-AC-013：其他用户的活动任务不阻塞本人删除。"""
    await client.post("/api/auth/register", json=credentials)
    session_id = (await open_session(client))["id"]
    await client.post("/api/auth/logout")
    await client.post("/api/auth/register", json=other_credentials)
    stranger = await _user_id(other_credentials)
    await _insert_run(stranger, RunStatus.RUNNING)
    await _insert_run(stranger, RunStatus.QUEUED)
    await client.post("/api/auth/logout")
    assert (await client.post("/api/auth/login", json=credentials)).status_code == 200

    preview = await deletion_preview(client, session_id)
    assert preview["blocked"] is False
    response = await confirm_delete(client, session_id, preview["preview_token"])
    assert response.status_code == 204
    assert (await client.get(f"/api/sessions/{session_id}")).status_code == 404


async def test_rename_unaffected_by_active_tasks(signed_in: httpx.AsyncClient):
    """LIB-FR-014（§7.1）：重命名与查看不受活动任务门禁影响。"""
    session = await open_session(signed_in)
    asset = session["current_asset_id"]
    started = await _start_generation(signed_in)
    assert started.status_code == 202

    renamed_session = await signed_in.patch(
        f"/api/sessions/{session['id']}", json={"title": "新名字"}
    )
    assert renamed_session.status_code == 200
    renamed_asset = await signed_in.patch(f"/api/assets/{asset}", json={"title": "主图"})
    assert renamed_asset.status_code == 200
    assert (await signed_in.get(f"/api/sessions/{session['id']}")).status_code == 200


# ---- 用户级锁临界区（WI 决策 1 / LIB-AC-015）----


async def test_deletion_waits_for_user_lock(signed_in: httpx.AsyncClient, credentials):
    """LIB-AC-015：删除临界区持有 WI 决策 1 的用户锁，外部持锁时删除必须阻塞等待。"""
    session = await open_session(signed_in)
    token = (await deletion_preview(signed_in, session["id"]))["preview_token"]
    user_id = await _user_id(credentials)

    async with SessionFactory() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:ns, hashtext(:user))"),
            {"ns": LOCK_NAMESPACE, "user": str(user_id)},
        )
        request = asyncio.create_task(confirm_delete(signed_in, session["id"], token))
        done, pending = await asyncio.wait({request}, timeout=2.0)
        assert request in pending, "会话删除未与同用户的登记/写入临界区互斥"

    response = await request
    assert response.status_code == 204
    assert (await signed_in.get(f"/api/sessions/{session['id']}")).status_code == 404


async def test_task_registration_waits_for_user_lock(signed_in: httpx.AsyncClient, credentials):
    """LIB-AC-015：任务登记（runs.create）与删除共用同一用户锁。"""
    user_id = await _user_id(credentials)

    async with SessionFactory() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:ns, hashtext(:user))"),
            {"ns": LOCK_NAMESPACE, "user": str(user_id)},
        )
        request = asyncio.create_task(_start_generation(signed_in))
        done, pending = await asyncio.wait({request}, timeout=2.0)
        assert request in pending, "任务登记未与同用户的删除临界区互斥"

    response = await request
    assert response.status_code == 202


# ---- 受控竞争：只允许合法顺序（PRD §7.1 / LIB-AC-015）----


async def test_tool_submission_and_session_deletion_race(signed_in: httpx.AsyncClient):
    """§7.1 / LIB-AC-015：提交与删除竞争只允许「任务先登记→删除 409」或
    「删除先提交→任务因资源不存在被拒且不入库」，不得出现悬空运行。"""
    session = await open_session(signed_in)
    token = (await deletion_preview(signed_in, session["id"]))["preview_token"]

    submit, delete = await asyncio.gather(
        signed_in.post(
            f"/api/sessions/{session['id']}/tools",
            json={"tool": "adjust_image", "params": {"brightness": 0.1}},
        ),
        confirm_delete(signed_in, session["id"], token),
    )

    assert not (delete.status_code == 204 and submit.status_code == 202), "出现悬空运行"
    if delete.status_code == 204:
        # 删除先提交：任务因资源不存在而拒绝，不入库、不投递
        assert submit.status_code == 404
        async with SessionFactory() as db:
            dangling = await db.scalar(
                select(func.count())
                .select_from(ToolRun)
                .where(ToolRun.session_id == uuid.UUID(session["id"]))
            )
        assert dangling == 0
        assert (await signed_in.get(f"/api/sessions/{session['id']}")).status_code == 404
    else:
        # 任务先登记：删除 409 active_tasks，任务与会话保持
        assert delete.status_code == 409
        assert delete.json()["detail"]["code"] == "active_tasks"
        assert submit.status_code == 202
        assert (await signed_in.get(f"/api/sessions/{session['id']}")).status_code == 200
        run_id = submit.json()["run"]["id"]
        assert (await signed_in.get(f"/api/runs/{run_id}")).json()["status"] == "queued"


async def test_reference_write_and_asset_deletion_race(signed_in: httpx.AsyncClient):
    """§7.1 / LIB-AC-015：引用写入与删除竞争只允许「引用先提交→asset_in_use」或
    「删除先提交→写入被拒」，不得出现悬空图层/历史引用。"""
    base = await upload(signed_in)
    candidate = await upload(signed_in)
    session = await open_session(signed_in, current_asset_id=base, asset_ids=[candidate])

    patch, delete = await asyncio.gather(
        signed_in.patch(
            f"/api/sessions/{session['id']}", json={"current_asset_id": candidate}
        ),
        signed_in.delete(f"/api/assets/{candidate}"),
    )

    assert not (patch.status_code == 200 and delete.status_code == 204), "出现悬空引用"
    if patch.status_code == 200:
        # 引用先提交 → asset_in_use
        assert delete.status_code == 409
        assert delete.json()["detail"]["code"] == "asset_in_use"
        assert (await signed_in.get(f"/api/assets/{candidate}")).status_code == 200
    else:
        # 删除先提交 → 引用写入被拒绝；会话无任何指向已删素材的悬空引用
        assert patch.status_code == 404
        assert delete.status_code == 204
        assert (await signed_in.get(f"/api/assets/{candidate}")).status_code == 404
        detail = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
        layer_assets = {
            layer["asset_id"] for layer in detail["document"]["layers"] if layer["asset_id"]
        }
        assert candidate not in layer_assets
        history = (await signed_in.get(f"/api/sessions/{session['id']}/history")).json()
        for entry in history:
            states = [entry.get("result") or {}, (entry.get("params") or {}).get("before") or {}]
            for state in states:
                assert state.get("current_asset_id") != candidate
                for layer in (state.get("document") or {}).get("layers") or []:
                    assert layer.get("asset_id") != candidate


# ---- 迟到 worker 与执行代次（WI 决策 2 / LIB-AC-015）----


def _patch_handler(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """把 adjust_image 的 handler 换成测试屏障：外部模型调用期间发生并发事件。"""
    spec = spec_of("adjust_image")
    monkeypatch.setitem(
        tools_registry._BY_NAME, "adjust_image", dataclasses.replace(spec, handler=handler)
    )


async def test_late_worker_cannot_write_after_cancel_and_session_delete(
    signed_in: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, credentials
):
    """§7.1 / LIB-AC-015：任务被取消、会话被删除后，旧 worker 的迟到结果不得
    创建资产、写引用或把任务改离终态。"""
    from app.models.asset import AssetKind, AssetSource
    from app.services import assets as asset_service
    from app.services import sessions as sessions_service

    if not hasattr(sessions_service, "delete_for_user"):
        pytest.fail("实现缺失：app.services.sessions.delete_for_user（WI 范围清单）")

    session = await open_session(signed_in)
    session_uuid = uuid.UUID(session["id"])
    user_id = await _user_id(credentials)
    before = await _asset_count(user_id)

    async def late_handler(db, run):
        # 模拟外部模型调用返回前：任务被取消，随后会话在删除临界区内被合法删除
        async with SessionFactory() as other:
            twin = await other.get(ToolRun, run.id)
            await runs_service.finish(other, twin, status=RunStatus.CANCELED, error="已取消")
        async with SessionFactory() as other:
            await sessions_service.delete_for_user(other, run.user_id, run.session_id)
        # 迟到的旧执行继续落产物：必须被写入资格校验丢弃
        await asset_service.create_from_bytes(
            db, run.user_id, make_image((64, 64)), AssetKind.GENERATED, AssetSource.GENERATE
        )
        return {"asset_ids": []}

    _patch_handler(monkeypatch, late_handler)
    body = await invoke(signed_in, session["id"], "adjust_image", {"brightness": 0.1})
    await run_tool({}, uuid.UUID(body["run"]["id"]))

    assert (await signed_in.get(f"/api/sessions/{session['id']}")).status_code == 404
    # 会话独占的底图随 D(X) 删除（before=1 → 0），迟到 worker 不得再创建任何资产
    assert await _asset_count(user_id) == before - 1, "迟到 worker 不得创建资产"
    assert await _history_count(session_uuid) == 0, "迟到 worker 不得重建历史"
    # run 行随会话级联清空（PRD LIB-AC-003）；迟到执行不得复活任务行
    async with SessionFactory() as db:
        resurrected = await db.get(ToolRun, uuid.UUID(body["run"]["id"]))
    assert resurrected is None, "迟到 worker 不得复活已级联删除的任务行"


async def test_late_worker_discarded_when_retry_takes_over(
    signed_in: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, credentials
):
    """§7.1 / LIB-AC-015：失联回收后重试的新执行（retries 递增）在跑时，
    旧执行代次的迟到写入必须被丢弃。"""
    from app.models.asset import AssetKind, AssetSource
    from app.services import assets as asset_service

    session = await open_session(signed_in)
    session_uuid = uuid.UUID(session["id"])
    user_id = await _user_id(credentials)
    assets_before = await _asset_count(user_id)
    history_before = await _history_count(session_uuid)

    async def stale_handler(db, run):
        # 模拟外部模型调用返回前：旧执行被收割，重试的新执行已开始（retries=2、RUNNING）
        async with SessionFactory() as other:
            twin = await other.get(ToolRun, run.id)
            await runs_service.finish(other, twin, status=RunStatus.FAILED, error="失联回收")
            await runs_service.start(other, twin)
        # 旧执行（代次=1）迟到回来写产物：不得取得写入资格
        await asset_service.create_from_bytes(
            db, run.user_id, make_image((64, 64)), AssetKind.GENERATED, AssetSource.GENERATE
        )
        return {"asset_ids": []}

    _patch_handler(monkeypatch, stale_handler)
    body = await invoke(signed_in, session["id"], "adjust_image", {"brightness": 0.1})
    await run_tool({}, uuid.UUID(body["run"]["id"]))

    assert await _asset_count(user_id) == assets_before, "旧代次写入必须被丢弃"
    assert await _history_count(session_uuid) == history_before, "旧执行不得写引用"
    async with SessionFactory() as db:
        run = await db.get(ToolRun, uuid.UUID(body["run"]["id"]))
    assert run.status is RunStatus.RUNNING, "旧执行不得把新执行标成终态"
    assert run.retries == 2


# ---- 批删的中途事件（PRD §8.2.4、§8.2.6 / LIB-AC-016）----


async def test_batch_delete_skips_items_when_task_appears_midway(
    signed_in: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, credentials
):
    """§8.2.4 / LIB-AC-016：请求级预检通过后新出现的活动任务只跳过尚未处理项，
    已提交项保留。屏障=第 1 项 DB 提交后的对象清理（storage.delete）。"""
    user_id = await _user_id(credentials)
    first = await upload(signed_in)
    second = await upload(signed_in)
    real_delete = storage.delete
    injected = False

    async def inject_then_delete(key: str) -> None:
        nonlocal injected
        if not injected:
            # 第 1 项已提交、进入对象清理阶段；此刻同用户新登记一个排队任务
            injected = True
            async with SessionFactory() as db:
                db.add(
                    ToolRun(
                        user_id=user_id,
                        session_id=None,
                        tool="generate_image",
                        status=RunStatus.QUEUED,
                        params={},
                        result={},
                        progress=0,
                        stage="",
                        retries=0,
                    )
                )
                await db.commit()
        await real_delete(key)

    monkeypatch.setattr("app.storage.delete", inject_then_delete)

    response = await signed_in.post(
        "/api/assets/batch-delete", json={"asset_ids": [first, second]}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] == [first]
    [skipped] = body["skipped"]
    assert skipped["id"] == second
    assert skipped["code"] == "active_tasks"
    assert (await signed_in.get(f"/api/assets/{first}")).status_code == 404
    assert (await signed_in.get(f"/api/assets/{second}")).status_code == 200


async def test_batch_delete_unexpected_error_keeps_committed_items(
    signed_in: httpx.AsyncClient,
):
    """§8.2.6 / LIB-AC-016：非预期 DB 异常停止后续处理、不伪装成功、不回滚已提交项。

    用守表对第 2 项素材加 RESTRICT 外键注入数据库层失败；transport 关闭异常上抛。
    """
    from httpx import ASGITransport

    from app.main import app

    first = await upload(signed_in)
    second = await upload(signed_in)
    guard = f"_lib_guard_{uuid.uuid4().hex[:8]}"
    try:
        async with SessionFactory() as db:
            await db.execute(
                text(
                    f"CREATE TABLE {guard} "
                    "(asset_id uuid NOT NULL REFERENCES assets(id) ON DELETE RESTRICT)"
                )
            )
            await db.execute(text(f"INSERT INTO {guard} (asset_id) VALUES (:a)"), {"a": second})
            await db.commit()

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as lenient:
            lenient.cookies.update(signed_in.cookies)
            response = await lenient.post(
                "/api/assets/batch-delete", json={"asset_ids": [first, second]}
            )
    finally:
        async with SessionFactory() as db:
            await db.execute(text(f"DROP TABLE IF EXISTS {guard}"))
            await db.commit()

    # 不伪装成 200/skipped；约束竞争按 §7 转 409，其他非预期异常按 §8.2.6 返回 500
    assert response.status_code in (409, 500)
    # 已提交项不回滚，失败项保留
    assert (await signed_in.get(f"/api/assets/{first}")).status_code == 404
    assert (await signed_in.get(f"/api/assets/{second}")).status_code == 200
