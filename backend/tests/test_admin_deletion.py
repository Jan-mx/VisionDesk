"""WI-20261025-03 冻结与账号永久清理专项测试（Red 阶段）。

对应测试矩阵 9 行（docs/work-items/WI-20261025-03-account-deletion.md），每条用例
docstring 标注所属矩阵行与验收场景（ADM-AC-021～034 逐条映射）。行 9（回归）由
既有套件承担：实现前基线 ``pytest tests/test_assets.py tests/test_deletion_safety.py
tests/test_deletion_concurrency.py -q`` 已全绿（68 passed），实现后跑全量门禁。

Red 与造数约定（沿用阶段 1/2 先例）：

- 阶段 3 新端点（deletion-preview / DELETE / deletion-operations）尚不存在：请求落入
  ``main.py`` 的 ``/api`` 兜底路由得到 404（detail 为字符串），每条 HTTP 用例在第一个
  端点断言上以具体断言失败呈现 Red；
- 新服务（``app.tasks.deletion``）在测试体内惰性导入，且每个触达点之前必有先行的
  端点断言或 :func:`_require_deletion_tables`（pytest.fail），保证实现前失败是
  断言失败/明确 fail，而非收集错误或裸的 UndefinedTable；
- 两张新表（account_deletion_operations / pending_object_writes）经 SQLAlchemy
  ``text()`` 直查直插（表结构以 WI 决策 1 为契约）；清理任务用测试直接调用服务层
  函数模拟 cron/worker（与 tests/test_deletion_concurrency.py 绕开真实 ARQ 同法：
  确认端点投递的任务无 worker 消费，由测试扮演执行者）；
- 管理员造数沿用阶段 2（注册 test_ 账号后 SQL 直提管理员）；被删目标一律 test_
  前缀账号，autouse 夹具自清 deleting 墓碑、删除操作行、登记行、目标审计行与 S3
  前缀残留，绝不触碰非测试数据；
- 开关 ``admin_account_deletion_enabled`` 实现前不存在：夹具以 ``hasattr`` 守卫，
  实现前为 no-op（Red 由端点 404 断言呈现），实现后显式置 True/False。

故障注入手法说明（复用既有 monkeypatch 先例，无法如实表达处用最接近的诚实变体）：

- S3 列举/删除失败：替换 ``app.storage._client``（app/storage.py 唯一的 boto3 客户端
  工厂，put/get/delete 均经它）为代理对象——委派其余方法、仅对 list_*/delete_*
  抛错。实现无论把列举/删除封装成什么名字，只要走 storage 模块即被拦截；
- 预览凭证过期：真实凭证签发后，把 ``app.security.datetime`` 替换为 +6min 的偏移
  子类再提交确认（过期判定发生在读取时，WI 决策 3 的 ``_preview_secret`` 同域在
  app.security；若实现把过期判定放到别处需同步调整注入点）；
- 写入屏障暂停点（登记后/PUT 后/提交前）：monkeypatch ``app.storage.put`` 与
  ``guards.assert_writable`` 在暂停点内注入冻结（SQL 直改 status='deleting'）。
  「提交前」窗口在生产中由用户锁串行化（确认流程拿不到锁就冻不了结），测试以
  SQL 绕锁注入等价时序并在用例内注明；该场景另需一个持久化操作行，由测试按
  WI 决策 1/3 的表契约 SQL 直插（绕开的只是确认端点的校验层，清理路径全部真实）。
"""

import asyncio
import dataclasses
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import text

import app.tools as tools_registry
from app import storage
from app.config import get_settings
from app.db import SessionFactory
from app.main import app
from app.models import AgentRun, ToolRun
from app.models.asset import AssetKind, AssetSource
from app.models.tool_run import RunStatus
from app.storage import _client as s3_client_factory
from app.tools import spec_of
from tests.canvas import invoke
from tests.test_assets import create_mask, make_image, open_session

# WI 决策 1 固定的用户级事务咨询锁命名空间（与 guards.LOCK_NAMESPACE 同值）
LOCK_NAMESPACE = 920620

# 本文件创建过的目标账号（含已删除的墓碑）：autouse 夹具据此自清全部残留
_TRACKED: set[uuid.UUID] = set()


def _value(raw):
    """text() 直查返回原始字符串；经枚举列映射读出的是枚举成员，统一还原为值。"""
    return getattr(raw, "value", raw)


async def _scalar(sql: str, **params):
    async with SessionFactory() as session:
        return (await session.execute(text(sql), params)).scalar_one()


async def _require_no_external_admin() -> None:
    """与 tests/test_admin_auth.py 同一前置：测试只增删 test_ 前缀账号。"""
    async with SessionFactory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT username FROM users"
                    " WHERE role = 'admin' AND LEFT(username, 5) <> 'test_'"
                )
            )
        ).scalars().all()
    if rows:
        pytest.fail(
            f"测试库存在非测试管理员账号 {rows}：测试不改写非测试数据，"
            "请先在隔离环境处理后再运行本组用例"
        )


async def _make_admin(client: httpx.AsyncClient, credentials: dict) -> dict:
    """注册 test_ 账号并 SQL 直提为管理员，返回 {"id", "username", "password"}。"""
    await _require_no_external_admin()
    assert (await client.post("/api/auth/register", json=credentials)).status_code == 201
    async with SessionFactory() as session:
        await session.execute(
            text("DELETE FROM users WHERE role = 'admin' AND username LIKE 'test_%'")
        )
        await session.execute(
            text("UPDATE users SET role = 'admin' WHERE username = :u"),
            {"u": credentials["username"]},
        )
        await session.commit()
    me = await client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    return {"id": me.json()["id"], "username": credentials["username"],
            "password": credentials["password"]}


async def _insert_user() -> tuple[uuid.UUID, str]:
    """SQL 直插 test_ 账号（无会话、无登录），返回 (id, username) 并登记自清。"""
    user_id = uuid.uuid4()
    username = f"test_del_{uuid.uuid4().hex[:12]}"
    async with SessionFactory() as session:
        await session.execute(
            text("INSERT INTO users (id, username, password_hash) VALUES (:id, :u, 'x')"),
            {"id": user_id, "u": username},
        )
        await session.commit()
    _TRACKED.add(user_id)
    return user_id, username


async def _track_client_user(client: httpx.AsyncClient) -> uuid.UUID:
    """登记经 API 注册的目标账号 id（其数据同样纳入自清范围）。"""
    response = await client.get("/api/auth/me")
    assert response.status_code == 200, response.text
    user_id = uuid.UUID(response.json()["id"])
    _TRACKED.add(user_id)
    return user_id


@pytest.fixture
async def admin_client():
    """独立于目标用户 ``client`` 的第二支客户端：承载管理员会话，互不串 Cookie。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def deletion_enabled(monkeypatch: pytest.MonkeyPatch):
    """打开清理开关（WI 决策 3：Settings.admin_account_deletion_enabled 默认 False）。

    实现前字段不存在，no-op——Red 由端点 404 断言呈现，而非夹具报错。
    """
    _set_switch(monkeypatch, True)


def _set_switch(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    settings = get_settings()
    if hasattr(settings, "admin_account_deletion_enabled"):
        monkeypatch.setattr(settings, "admin_account_deletion_enabled", value)


@pytest.fixture(autouse=True)
async def purge_deletion_artifacts():
    """每个用例后自清：目标 S3 前缀（含版本）、删除操作/登记/审计行。

    新表实现前不存在，用 to_regclass 守卫（Red 阶段不产生清理噪音）；
    users 行（含 deleting 墓碑）由 conftest 的 cleanup_users 统一删除。
    """
    _TRACKED.clear()
    yield
    for user_id in list(_TRACKED):
        try:
            await _scrub_user_prefix(user_id)
        except Exception:  # noqa: BLE001 — S3 残留清理尽力而为，不掩盖断言结果
            pass
    if _TRACKED:
        operations = await _scalar(
            "SELECT to_regclass('account_deletion_operations') IS NOT NULL"
        )
        pending = await _scalar("SELECT to_regclass('pending_object_writes') IS NOT NULL")
        for user_id in list(_TRACKED):
            async with SessionFactory() as session:
                if pending:
                    await session.execute(
                        text("DELETE FROM pending_object_writes WHERE user_id = :u"),
                        {"u": user_id},
                    )
                if operations:
                    await session.execute(
                        text(
                            "DELETE FROM account_deletion_operations"
                            " WHERE target_user_id = :u"
                        ),
                        {"u": user_id},
                    )
                await session.execute(
                    text(
                        "DELETE FROM admin_audit_logs"
                        " WHERE target_type = 'user' AND target_id = :u"
                    ),
                    {"u": user_id},
                )
                await session.commit()
    _TRACKED.clear()


# ---- S3 前缀 oracle 与自清（boto3 直查，不经实现）----


def _bucket() -> str:
    return get_settings().s3_bucket


def _prefix_of(user_id: uuid.UUID) -> str:
    # PRD §10：精确前缀末尾保留斜杠
    return f"users/{user_id}/"


def _prefix_keys(user_id: uuid.UUID) -> list[str]:
    response = s3_client_factory().list_objects_v2(
        Bucket=_bucket(), Prefix=_prefix_of(user_id)
    )
    return sorted(item["Key"] for item in response.get("Contents", []))


def _prefix_versions(user_id: uuid.UUID) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(版本对象, 删除标记) 列表，键内含 VersionId——AC-027 的版本化桶 oracle。"""
    response = s3_client_factory().list_object_versions(
        Bucket=_bucket(), Prefix=_prefix_of(user_id)
    )
    versions = [(v["Key"], v["VersionId"]) for v in response.get("Versions", [])]
    markers = [(m["Key"], m["VersionId"]) for m in response.get("DeleteMarkers", [])]
    return sorted(versions), sorted(markers)


async def _scrub_user_prefix(user_id: uuid.UUID) -> None:
    """删除目标前缀下全部版本与删除标记（测试自清；对未版本化桶同样适用）。"""
    versions, markers = await asyncio.to_thread(_prefix_versions, user_id)
    client = s3_client_factory()
    for key, version_id in [*versions, *markers]:
        await asyncio.to_thread(
            client.delete_object, Bucket=_bucket(), Key=key, VersionId=version_id
        )


# ---- 新表/操作/确认 流程助手 ----


async def _require_deletion_tables() -> None:
    """直插新表前的前置检查：缺失时以明确 fail 呈现 Red（而非裸 UndefinedTable）。"""
    missing = [
        name
        for name in ("account_deletion_operations", "pending_object_writes")
        if not await _scalar(f"SELECT to_regclass('{name}') IS NOT NULL")
    ]
    if missing:
        pytest.fail(f"实现缺失：新表 {missing} 尚不存在（WI 决策 1）")


async def _op_row(operation_id: uuid.UUID) -> dict:
    async with SessionFactory() as session:
        row = await session.execute(
            text(
                "SELECT id::text, target_user_id::text AS target_user_id,"
                " status::text AS status, phase::text AS phase, progress_done,"
                " progress_total, attempt_count, failure_code,"
                " COALESCE(failure_detail, '') AS failure_detail, finished_at"
                " FROM account_deletion_operations WHERE id = :i"
            ),
            {"i": operation_id},
        )
        return dict(row.mappings().one())


async def _op_count(target_id: uuid.UUID) -> int:
    return await _scalar(
        "SELECT count(*) FROM account_deletion_operations WHERE target_user_id = :u",
        u=target_id,
    )


async def _user_row(user_id: uuid.UUID) -> dict:
    async with SessionFactory() as session:
        row = await session.execute(
            text(
                "SELECT username, status::text AS status, token_version"
                " FROM users WHERE id = :u"
            ),
            {"u": user_id},
        )
        return dict(row.mappings().one())


async def _count(table: str, user_id: uuid.UUID) -> int:
    return await _scalar(f"SELECT count(*) FROM {table} WHERE user_id = :u", u=user_id)


async def _db_storage_keys(user_id: uuid.UUID) -> set[str]:
    async with SessionFactory() as session:
        rows = await session.execute(
            text("SELECT storage_key FROM assets WHERE user_id = :u"), {"u": user_id}
        )
        return set(rows.scalars())


async def _wall_count(user_id: uuid.UUID) -> int:
    """墙行数（session_assets 无 user_id 列，经会话归属关联）。"""
    return await _scalar(
        "SELECT count(*) FROM session_assets sa"
        " JOIN edit_sessions es ON es.id = sa.session_id WHERE es.user_id = :u",
        u=user_id,
    )


async def _drive_cleanup(operation_id: uuid.UUID, max_rounds: int = 40) -> dict:
    """直接调用服务层清理任务模拟 cron/worker，循环推进直到终态（同 op 断点续跑）。"""
    from app.tasks.deletion import cleanup_account  # 惰性导入：实现前模块缺失

    row: dict = {}
    for _ in range(max_rounds):
        row = await _op_row(operation_id)
        if _value(row["status"]) != "processing":
            return row
        await cleanup_account({}, operation_id)
    pytest.fail(
        f"cleanup_account 循环 {max_rounds} 轮后仍未终态（operation={operation_id}，"
        f"phase={row.get('phase')}）"
    )


async def _full_delete_flow(
    admin_client: httpx.AsyncClient, target_id: uuid.UUID, username: str
) -> uuid.UUID:
    """预览 → 凭证 → 确认删除，返回 operation id（实现前在预览断言上 Red）。"""
    preview = await admin_client.get(f"/api/admin/users/{target_id}/deletion-preview")
    assert preview.status_code == 200, preview.text
    token = preview.json()["preview_token"]
    assert token, "非阻断预览必须签发可执行凭证"
    response = await admin_client.request("DELETE", 
        f"/api/admin/users/{target_id}",
        headers={"X-Deletion-Preview": token},
        json={"confirm_username": username},
    )
    assert response.status_code == 202, response.text
    return uuid.UUID(response.json()["id"])


async def _freeze_user(user_id: uuid.UUID) -> None:
    """SQL 直改 status='deleting'（屏障只读 users.status，WI 决策 2a/2c）。"""
    async with SessionFactory() as session:
        await session.execute(
            text("UPDATE users SET status = 'deleting' WHERE id = :u"), {"u": user_id}
        )
        await session.commit()


async def _pending_states(user_id: uuid.UUID) -> list[str]:
    async with SessionFactory() as session:
        rows = await session.execute(
            text("SELECT state FROM pending_object_writes WHERE user_id = :u"),
            {"u": user_id},
        )
        return [_value(row) for row in rows.scalars()]


async def _plant_pending_write(
    user_id: uuid.UUID, storage_key: str, *, age_seconds: int = 0
) -> None:
    """按 WI 决策 1 的表契约直插一条 registered 登记行（age_seconds 模拟崩溃残留）。"""
    async with SessionFactory() as session:
        await session.execute(
            text(
                "INSERT INTO pending_object_writes"
                " (id, user_id, storage_key, run_id, retries, state, created_at)"
                " VALUES (:id, :u, :k, NULL, NULL, 'registered',"
                " now() - make_interval(secs => :age))"
            ),
            {"id": uuid.uuid4(), "u": user_id, "k": storage_key, "age": age_seconds},
        )
        await session.commit()


async def _plant_operation(target_id: uuid.UUID) -> uuid.UUID:
    """按 WI 决策 1/3 的表契约直插一个 processing/frozen 操作行。

    仅用于「冻结已由确认流程提交」不可经 HTTP 复现的提交前竞争场景（见模块
    docstring）；清理路径（cleanup_account）全部真实执行。
    """
    operation_id = uuid.uuid4()
    async with SessionFactory() as session:
        await session.execute(
            text(
                "INSERT INTO account_deletion_operations"
                " (id, target_user_id, actor_id, status, phase, updated_at)"
                " VALUES (:id, :t, :a, 'processing', 'frozen', now())"
            ),
            {"id": operation_id, "t": target_id, "a": uuid.uuid4()},
        )
        await session.commit()
    return operation_id


async def _audit_rows_for_target(target_id: uuid.UUID) -> list[dict]:
    async with SessionFactory() as session:
        rows = await session.execute(
            text(
                "SELECT action, result, COALESCE(operation_id::text, '') AS operation_id,"
                " COALESCE(summary::text, '') AS summary"
                " FROM admin_audit_logs WHERE target_type = 'user' AND target_id = :t"
                " ORDER BY created_at, id"
            ),
            {"t": target_id},
        )
        return [dict(row._mapping) for row in rows]


async def _audit_rows_for_operation(operation_id: uuid.UUID) -> list[dict]:
    async with SessionFactory() as session:
        rows = await session.execute(
            text(
                "SELECT action, result, target_id::text AS target_id,"
                " COALESCE(summary::text, '') AS summary"
                " FROM admin_audit_logs WHERE operation_id = :o"
                " ORDER BY created_at, id"
            ),
            {"o": operation_id},
        )
        return [dict(row._mapping) for row in rows]


# ---- 造数助手（直插业务行，绕开 API 速率；造数前提在用例内自检）----


async def _seed_asset(
    user_id: uuid.UUID,
    *,
    kind: str = "original",
    source: str = "upload",
    library_hidden: bool = False,
    size_bytes: int = 256,
    with_object: bool = False,
    session_id: uuid.UUID | None = None,
    position: int | None = None,
) -> tuple[uuid.UUID, str]:
    """直插一条素材（可选挂墙、可选真实写入 S3 对象），返回 (asset_id, storage_key)。"""
    asset_id = uuid.uuid4()
    key = f"users/{user_id}/{asset_id}.png"
    async with SessionFactory() as session:
        await session.execute(
            text(
                "INSERT INTO assets (id, user_id, kind, source, storage_key, image_format,"
                " width, height, size_bytes, has_alpha, library_hidden)"
                " VALUES (:id, :u, :kind, :source, :key, 'PNG', 64, 64, :size, false, :hidden)"
            ),
            {"id": asset_id, "u": user_id, "kind": kind, "source": source,
             "key": key, "size": size_bytes, "hidden": library_hidden},
        )
        if session_id is not None and position is not None:
            await session.execute(
                text(
                    "INSERT INTO session_assets (session_id, asset_id, position)"
                    " VALUES (:s, :a, :p)"
                ),
                {"s": session_id, "a": asset_id, "p": position},
            )
        await session.commit()
    if with_object:
        await storage.put(key, make_image((64, 64)), "image/png")
    return asset_id, key


async def _seed_session(user_id: uuid.UUID, asset_id: uuid.UUID) -> uuid.UUID:
    """直插一条引用 asset 的会话（original/current 同指，画布空文档）。"""
    session_id = uuid.uuid4()
    async with SessionFactory() as session:
        await session.execute(
            text(
                "INSERT INTO edit_sessions (id, user_id, title, original_asset_id,"
                " current_asset_id, revision, history_seq, document)"
                " VALUES (:id, :u, '测试会话', :a, :a, 1, 1, '{}'::jsonb)"
            ),
            {"id": session_id, "u": user_id, "a": asset_id},
        )
        await session.commit()
    return session_id


async def _seed_tool_run(user_id: uuid.UUID, status: str) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with SessionFactory() as session:
        await session.execute(
            text(
                "INSERT INTO tool_runs (id, user_id, session_id, tool, status, progress,"
                " stage, params, result, retries)"
                " VALUES (:id, :u, NULL, 'generate_image', :status, 100, 'done',"
                " '{}'::jsonb, '{}'::jsonb, 0)"
            ),
            {"id": run_id, "u": user_id, "status": status},
        )
        await session.commit()
    return run_id


async def _seed_agent_run(
    user_id: uuid.UUID, session_id: uuid.UUID, status: str
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with SessionFactory() as session:
        session.add(
            AgentRun(
                id=run_id,
                user_id=user_id,
                session_id=session_id,
                revision=1,
                goal="测试轮次",
                plan=[],
                status=status,
            )
        )
        await session.commit()
    return run_id


async def _seed_history(user_id: uuid.UUID, session_id: uuid.UUID, seq: int) -> None:
    async with SessionFactory() as session:
        await session.execute(
            text(
                "INSERT INTO edit_history (id, user_id, session_id, seq, action, params, result)"
                " VALUES (:id, :u, :s, :seq, 'note', '{}'::jsonb, '{}'::jsonb)"
            ),
            {"id": uuid.uuid4(), "u": user_id, "s": session_id, "seq": seq},
        )
        await session.commit()


def _patch_adjust_handler(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """把 adjust_image 的 handler 换成测试屏障（与 test_deletion_concurrency 同法）。"""
    spec = spec_of("adjust_image")
    monkeypatch.setitem(
        tools_registry._BY_NAME, "adjust_image", dataclasses.replace(spec, handler=handler)
    )


# ===========================================================================
# 矩阵行 1（ADM-FR-011 / ADM-AC-021、022）：全量预览
# ===========================================================================


async def test_preview_counts_full_scope_beyond_list_limits(
    admin_client, credentials, deletion_enabled
):
    """矩阵行 1（ADM-FR-011 / ADM-AC-021）：预览全量计数与 DB/S3 实况一致——
    51 个会话（超任何列表页上限）、全 kind 含隐藏与内部派生、S3 孤儿对象；
    预览零副作用（无审计、无操作行、无状态/对象变化）。"""
    admin = await _make_admin(admin_client, credentials)
    user_id, username = await _insert_user()

    # 51 会话 + 各自原图（RESTRICT 引用成立），其中 3 张真实写入 S3
    session_ids = []
    for index in range(51):
        asset_id, _ = await _seed_asset(
            user_id, size_bytes=128 * (index + 1), with_object=index < 3
        )
        session_ids.append(await _seed_session(user_id, asset_id))
    # 全 kind：内部派生 + 导出 + 营销 + 生成；快照为隐藏（统计必须计入）
    await _seed_asset(user_id, kind="mask", source="tool")
    await _seed_asset(user_id, kind="subject", source="tool")
    await _seed_asset(user_id, kind="background", source="tool")
    await _seed_asset(user_id, kind="snapshot", source="tool", library_hidden=True)
    await _seed_asset(user_id, kind="export", source="tool")
    await _seed_asset(user_id, kind="marketing", source="tool")
    await _seed_asset(user_id, kind="generated", source="generate")
    # 历史 3 条 + 两类任务各 1（均终态，不阻断）
    for seq in (1, 2, 3):
        await _seed_history(user_id, session_ids[0], seq)
    await _seed_tool_run(user_id, "succeeded")
    await _seed_agent_run(user_id, session_ids[0], "succeeded")
    # 孤儿对象：前缀下真实存在但无 DB 行
    for suffix in ("a", "b"):
        await storage.put(
            f"users/{user_id}/orphan-{suffix}-{uuid.uuid4().hex}.bin",
            b"orphan",
            "application/octet-stream",
        )

    # oracle：以同一口径的 SQL/S3 实况为预期基准（不硬编码假设）
    keys = await asyncio.to_thread(_prefix_keys, user_id)
    db_keys = await _db_storage_keys(user_id)
    expected = {
        "sessions": await _count("edit_sessions", user_id),
        "history": await _count("edit_history", user_id),
        "tool_runs": await _count("tool_runs", user_id),
        "agent_runs": await _count("agent_runs", user_id),
        "assets": await _count("assets", user_id),
        "asset_record_bytes": await _scalar(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM assets WHERE user_id = :u", u=user_id
        ),
        "objects": len(keys),
        "orphan_objects": len([key for key in keys if key not in db_keys]),
    }
    assert expected["sessions"] == 51  # 造数确实超出任何列表页上限
    assert expected["orphan_objects"] == 2

    audit_before = await _scalar(
        "SELECT count(*) FROM admin_audit_logs WHERE actor_id = :a", a=uuid.UUID(admin["id"])
    )

    response = await admin_client.get(f"/api/admin/users/{user_id}/deletion-preview")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user_id"] == str(user_id)
    assert body["username"] == username
    assert body["blocked"] is False
    assert body["preview_token"]  # 非阻断必须签发可执行凭证
    assert body["expires_at"]
    counts = body["counts"]
    for field, value in expected.items():
        assert counts[field] == value, f"counts.{field}: {counts[field]} != {value}"

    # 预览零副作用：重复预览计数稳定，且不留任何写入痕迹
    again = await admin_client.get(f"/api/admin/users/{user_id}/deletion-preview")
    assert again.status_code == 200, again.text
    assert again.json()["counts"] == counts
    assert await asyncio.to_thread(_prefix_keys, user_id) == keys
    assert await _scalar(
        "SELECT count(*) FROM admin_audit_logs WHERE actor_id = :a", a=uuid.UUID(admin["id"])
    ) == audit_before
    if await _scalar("SELECT to_regclass('account_deletion_operations') IS NOT NULL"):
        assert await _op_count(user_id) == 0
    assert (await _user_row(user_id))["status"] == "active"


async def test_preview_blocked_by_active_tasks_has_no_executable_token(
    admin_client, credentials, deletion_enabled
):
    """矩阵行 1（ADM-FR-011 / ADM-AC-022）：queued/running 工具与待确认计划
    一律 blocked、无可执行 token、blockers 可读；预览不修改任务状态。"""
    await _make_admin(admin_client, credentials)

    async def seed_pending_plan(user_id: uuid.UUID) -> tuple[uuid.UUID, str]:
        asset_id, _ = await _seed_asset(user_id)
        session_id = await _seed_session(user_id, asset_id)
        return await _seed_agent_run(user_id, session_id, "queued"), "agent_runs"

    async def seed_tool(user_id: uuid.UUID, status: str) -> tuple[uuid.UUID, str]:
        return await _seed_tool_run(user_id, status), "tool_runs"

    cases = {
        "queued-tool": lambda uid: seed_tool(uid, "queued"),
        "running-tool": lambda uid: seed_tool(uid, "running"),
        "pending-plan": seed_pending_plan,
    }

    for name, seed in cases.items():
        user_id, _ = await _insert_user()
        run_id, table = await seed(user_id)

        response = await admin_client.get(f"/api/admin/users/{user_id}/deletion-preview")

        assert response.status_code == 200, f"{name}: {response.text}"
        body = response.json()
        assert body["blocked"] is True, name
        assert body["preview_token"] is None, f"{name}: blocked 预览不得签发可执行凭证"
        assert body["blockers"], f"{name}: 阻断项必须可读"
        assert all(blocker.get("message") for blocker in body["blockers"]), name
        # 只读预览不修正任务状态（ADM-AC-022）
        status = await _scalar(f"SELECT status::text FROM {table} WHERE id = :r", r=run_id)
        assert _value(status) == ("queued" if name != "running-tool" else "running"), name


async def test_preview_of_owner_is_owner_protected(admin_client, credentials, deletion_enabled):
    """矩阵行 1 边界（WI 决策 3）：目标==自身 → 409 owner_protected，零写入。"""
    admin = await _make_admin(admin_client, credentials)

    response = await admin_client.get(f"/api/admin/users/{admin['id']}/deletion-preview")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "owner_protected"
    assert (await _user_row(uuid.UUID(admin["id"])))["status"] == "active"


async def test_preview_storage_listing_failure_returns_503_without_token(
    admin_client, credentials, monkeypatch, deletion_enabled
):
    """矩阵行 1（ADM-FR-011 / ADM-AC-022）：对象列举失败 → 503 storage_unavailable、
    不签发凭证、无审计零写入。注入点为 app.storage._client（storage 模块唯一的
    boto3 客户端工厂，任何列举实现都必经它）。"""
    admin = await _make_admin(admin_client, credentials)
    user_id, _ = await _insert_user()
    await storage.put(
        f"users/{user_id}/probe-{uuid.uuid4().hex}.bin", b"x", "application/octet-stream"
    )
    real_client = s3_client_factory()

    class BrokenListing:
        def __getattr__(self, name):
            return getattr(real_client, name)

        def list_objects_v2(self, **kwargs):
            raise RuntimeError("s3 listing down (injected)")

        def list_object_versions(self, **kwargs):
            raise RuntimeError("s3 listing down (injected)")

    monkeypatch.setattr("app.storage._client", lambda *args, **kwargs: BrokenListing())
    audit_before = await _scalar(
        "SELECT count(*) FROM admin_audit_logs WHERE actor_id = :a", a=uuid.UUID(admin["id"])
    )

    response = await admin_client.get(f"/api/admin/users/{user_id}/deletion-preview")

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, dict), detail
    assert detail["code"] == "storage_unavailable"
    assert "preview_token" not in response.text  # 失败响应不得携带任何凭证
    assert await _scalar(
        "SELECT count(*) FROM admin_audit_logs WHERE actor_id = :a", a=uuid.UUID(admin["id"])
    ) == audit_before  # 预览失败零审计
    assert (await _user_row(user_id))["status"] == "active"


# ===========================================================================
# 矩阵行 2（ADM-FR-012 / ADM-AC-023、024、025）：凭证确认与冻结
# ===========================================================================


async def test_confirm_rejects_forged_or_mismatched_credentials_without_freezing(
    admin_client, client, credentials, other_credentials, deletion_enabled
):
    """矩阵行 2（ADM-FR-012 / ADM-AC-023）：伪造凭证、异操作者、异目标 422，
    错确认名 409；全部零冻结零操作。异操作者经「降 A 提 B」在唯一管理员约束下
    用真实凭证构造（操作者绑定校验），不伪造载荷格式。"""
    admin_a = await _make_admin(admin_client, credentials)
    # 第二管理员 B：先以普通用户注册拿到会话（唯一管理员约束下逐条换角色）
    assert (await client.post("/api/auth/register", json=other_credentials)).status_code == 201
    admin_b_id = await _track_client_user(client)
    target_id, target_name = await _insert_user()
    other_id, other_name = await _insert_user()

    preview = await admin_client.get(f"/api/admin/users/{target_id}/deletion-preview")
    assert preview.status_code == 200, preview.text
    token = preview.json()["preview_token"]

    # 换操作者：A 降为普通、B 提为管理员（B 的旧会话按角色逐请求重读仍有效）
    async with SessionFactory() as session:
        await session.execute(
            text("UPDATE users SET role = 'user' WHERE id = :i"),
            {"i": uuid.UUID(admin_a["id"])},
        )
        await session.commit()
        await session.execute(
            text("UPDATE users SET role = 'admin' WHERE id = :i"), {"i": admin_b_id}
        )
        await session.commit()

    cases = [
        # (名称, 目标, 凭证, 确认用户名, 期望码)
        ("forged", target_id, "forged.signature-value", target_name, 422),
        ("wrong-operator", target_id, token, target_name, 422),  # B 提交 A 签发的凭证
        ("wrong-target", other_id, token, other_name, 422),  # 目标 A 的凭证打在 B 身上
        ("wrong-username", target_id, token, f"not-{target_name}", 409),
    ]
    for name, victim, credential, confirm_name, expected in cases:
        response = await client.request("DELETE", 
            f"/api/admin/users/{victim}",
            headers={"X-Deletion-Preview": credential},
            json={"confirm_username": confirm_name},
        )
        assert response.status_code == expected, f"{name}: {response.text}"
        if expected == 409:
            assert response.json()["detail"]["code"] == "delete_preview_changed", name

    # 全部拒绝零冻结零操作
    assert (await _user_row(target_id))["status"] == "active"
    assert (await _user_row(other_id))["status"] == "active"
    assert await _op_count(target_id) == 0
    assert await _op_count(other_id) == 0


async def test_confirm_expired_preview_token_returns_409(
    admin_client, credentials, monkeypatch
):
    """矩阵行 2（ADM-FR-012 / ADM-AC-023）：过期凭证 409 delete_preview_expired、
    零冻结。TTL 5 分钟不可等待：确认请求期间把 app.security.datetime 偏移 +6 分钟，
    使读取时的过期判定真实触发（签发仍是真实时刻）。"""
    import app.security as security_module

    _set_switch(monkeypatch, True)
    await _make_admin(admin_client, credentials)
    user_id, username = await _insert_user()

    preview = await admin_client.get(f"/api/admin/users/{user_id}/deletion-preview")
    assert preview.status_code == 200, preview.text
    token = preview.json()["preview_token"]

    real_datetime = security_module.datetime

    class ShiftedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return super().now(tz) + timedelta(minutes=6)

    monkeypatch.setattr(security_module, "datetime", ShiftedDatetime)
    try:
        response = await admin_client.request("DELETE", 
            f"/api/admin/users/{user_id}",
            headers={"X-Deletion-Preview": token},
            json={"confirm_username": username},
        )
    finally:
        monkeypatch.setattr(security_module, "datetime", real_datetime)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "delete_preview_expired"
    assert (await _user_row(user_id))["status"] == "active"
    assert await _op_count(user_id) == 0


async def test_confirm_scope_changed_requires_repreview(
    admin_client, client, credentials, other_credentials, deletion_enabled
):
    """矩阵行 2（ADM-FR-012 / ADM-AC-024）：预览后新增引用/改名/新活动任务，
    确认一律 409 要求重新预览，零冻结。锁内重读的时序由下一条用例专门验证。"""
    await _make_admin(admin_client, credentials)

    async def new_reference(user_id: uuid.UUID) -> None:
        # 目标本人新增素材与会话引用（client 即目标会话）
        asset_id, _ = await _seed_asset(user_id, with_object=True)
        response = await client.post("/api/sessions", json={"current_asset_id": str(asset_id)})
        assert response.status_code == 201, response.text

    async def renamed(user_id: uuid.UUID) -> None:
        response = await admin_client.patch(
            f"/api/admin/users/{user_id}",
            json={"username": f"test_renamed_{uuid.uuid4().hex[:8]}"},
        )
        assert response.status_code == 200, response.text

    async def new_active_task(user_id: uuid.UUID) -> None:
        await _seed_tool_run(user_id, "queued")

    for name, mutate in (
        ("new-reference", new_reference),
        ("renamed", renamed),
        ("new-active-task", new_active_task),
    ):
        # 每轮独立目标（用户名不可复用）
        target_credentials = {
            "username": f"test_scope_{uuid.uuid4().hex[:10]}",
            "password": "secret123",
        }
        assert (await client.post("/api/auth/register", json=target_credentials)).status_code == 201
        user_id = await _track_client_user(client)

        preview = await admin_client.get(f"/api/admin/users/{user_id}/deletion-preview")
        assert preview.status_code == 200, f"{name}: {preview.text}"
        token = preview.json()["preview_token"]

        await mutate(user_id)

        response = await admin_client.request("DELETE", 
            f"/api/admin/users/{user_id}",
            headers={"X-Deletion-Preview": token},
            json={"confirm_username": target_credentials["username"]},
        )
        assert response.status_code == 409, f"{name}: {response.text}"
        detail = response.json()["detail"]
        if name == "new-active-task":
            assert detail["code"] == "active_tasks", f"{name}: {detail}"
        else:
            assert detail["code"] == "delete_preview_changed", f"{name}: {detail}"
        # 零冻结
        assert await _op_count(user_id) == 0, name
        if name != "renamed":
            assert (await _user_row(user_id))["status"] == "active", name


async def test_confirm_rechecks_scope_inside_user_lock(
    admin_client, client, credentials, other_credentials, monkeypatch, deletion_enabled
):
    """矩阵行 2（ADM-FR-012 / ADM-AC-024）：确认在目标用户锁内重读范围——
    确认被同用户外部持锁者阻塞期间，锁内提交改名；确认恢复后必须在锁内发现
    用户名已变（409 delete_preview_changed），而非用阻塞前的旧读结果放行。"""
    _set_switch(monkeypatch, True)
    await _make_admin(admin_client, credentials)
    assert (await client.post("/api/auth/register", json=other_credentials)).status_code == 201
    user_id = await _track_client_user(client)

    preview = await admin_client.get(f"/api/admin/users/{user_id}/deletion-preview")
    assert preview.status_code == 200, preview.text
    token = preview.json()["preview_token"]

    async with SessionFactory() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:ns, hashtext(:uid))"),
            {"ns": LOCK_NAMESPACE, "uid": str(user_id)},
        )
        request = asyncio.create_task(
            admin_client.request("DELETE", 
                f"/api/admin/users/{user_id}",
                headers={"X-Deletion-Preview": token},
                json={"confirm_username": other_credentials["username"]},
            )
        )
        done, pending = await asyncio.wait({request}, timeout=2.0)
        assert request in pending, "确认未与同用户临界区互斥：锁内重读无法保证"
        # 锁内提交范围变化（改名）；提交即释放事务级锁
        await holder.execute(
            text("UPDATE users SET username = :n WHERE id = :u"),
            {"n": f"test_renamed_{uuid.uuid4().hex[:8]}", "u": user_id},
        )
        await holder.commit()

    response = await request
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "delete_preview_changed"
    assert await _op_count(user_id) == 0


async def test_confirm_freezes_account_and_deduplicates_submissions(
    admin_client, client, credentials, other_credentials, deletion_enabled
):
    """矩阵行 2（ADM-FR-012 / ADM-AC-025）：合法确认冻结账号（deleting + 会话版本
    递增 + 旧会话 401），重复与并发提交仅一个 operation 且幂等返回已有操作。"""
    await _make_admin(admin_client, credentials)
    assert (await client.post("/api/auth/register", json=other_credentials)).status_code == 201
    user_id = await _track_client_user(client)
    username = other_credentials["username"]
    version_before = (await _user_row(user_id))["token_version"]
    assert (await client.get("/api/auth/me")).status_code == 200  # 造数前提：会话有效

    preview = await admin_client.get(f"/api/admin/users/{user_id}/deletion-preview")
    assert preview.status_code == 200, preview.text
    token = preview.json()["preview_token"]

    first = await admin_client.request("DELETE", 
        f"/api/admin/users/{user_id}",
        headers={"X-Deletion-Preview": token},
        json={"confirm_username": username},
    )
    assert first.status_code == 202, first.text
    operation_id = first.json()["id"]

    # 冻结立即生效：deleting + token_version 递增 + 旧会话 401
    row = await _user_row(user_id)
    assert _value(row["status"]) == "deleting"
    assert row["token_version"] == version_before + 1
    assert (await client.get("/api/auth/me")).status_code == 401

    # 重复提交（同一凭证）：幂等返回已有操作，不重复执行
    duplicate = await admin_client.request("DELETE", 
        f"/api/admin/users/{user_id}",
        headers={"X-Deletion-Preview": token},
        json={"confirm_username": username},
    )
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["id"] == operation_id

    # 并发提交：仍只有一个 operation，双方返回同一 id
    concurrent_first, concurrent_second = await asyncio.gather(
        admin_client.request("DELETE", 
            f"/api/admin/users/{user_id}",
            headers={"X-Deletion-Preview": token},
            json={"confirm_username": username},
        ),
        admin_client.request("DELETE", 
            f"/api/admin/users/{user_id}",
            headers={"X-Deletion-Preview": token},
            json={"confirm_username": username},
        ),
    )
    assert concurrent_first.status_code == 202, concurrent_first.text
    assert concurrent_second.status_code == 202, concurrent_second.text
    assert concurrent_first.json()["id"] == operation_id
    assert concurrent_second.json()["id"] == operation_id
    assert await _op_count(user_id) == 1
    # user_delete 留痕含 operation_id（矩阵行 7 的确认半边）
    rows = await _audit_rows_for_operation(uuid.UUID(operation_id))
    assert any(row["action"] == "user_delete" for row in rows), rows


# ===========================================================================
# 矩阵行 3（ADM-FR-013 / ADM-AC-026、027）：全类型清理与前缀隔离
# ===========================================================================


async def test_cleanup_purges_every_kind_of_business_row(
    admin_client, client, credentials, other_credentials, deletion_enabled
):
    """矩阵行 3（ADM-FR-013 / ADM-AC-026）：原图/生成/遮罩/主体/背景/快照/导出/
    营销/墙/历史/两类任务的全类型账号清理后业务行全零、无 RESTRICT 报错、墓碑删除；
    其他用户的行与对象不受扰。"""
    from app.tasks.tools import run_tool

    await _make_admin(admin_client, credentials)
    assert (await client.post("/api/auth/register", json=other_credentials)).status_code == 201
    user_id = await _track_client_user(client)

    # 真实链路：会话（原图上墙）+ 派生遮罩 + 两次编辑（历史与派生产物）+ 生成任务收尾
    session = await open_session(client)
    await create_mask(client, session)
    # invoke 仅入队；编辑必须真正执行才有历史与派生产物（活动任务会阻断预览，
    # 与矩阵行 1 的 queued 阻断语义一致，故此处就地跑完，与本文件迟到 Worker
    # 用例的 invoke + run_tool 同法）
    first_edit = await invoke(client, session["id"], "adjust_image", {"brightness": 0.1})
    await run_tool({}, uuid.UUID(first_edit["run"]["id"]))
    second_edit = await invoke(client, session["id"], "adjust_image", {"brightness": 0.2})
    await run_tool({}, uuid.UUID(second_edit["run"]["id"]))
    generation = await client.post(
        "/api/generations",
        json={"prompt": "浅木色桌面上的白色马克杯", "ratio": "1:1", "count": 1},
    )
    assert generation.status_code == 202, generation.text
    await run_tool({}, uuid.UUID(generation.json()["id"]))
    # DB 直补齐 API 造不出的 kind，并挂墙形成 SessionAsset 引用
    wall_session = uuid.UUID(session["id"])
    for position, kind in enumerate(
        ("subject", "background", "snapshot", "export", "marketing"), start=2
    ):
        await _seed_asset(
            user_id, kind=kind, source="tool", session_id=wall_session, position=position
        )
    await _seed_agent_run(user_id, wall_session, "succeeded")
    # 孤儿对象一并清理
    await storage.put(
        f"users/{user_id}/orphan-{uuid.uuid4().hex}.bin", b"o", "application/octet-stream"
    )

    # 对照用户：行与对象都必须原样
    other_id, _ = await _insert_user()
    other_asset, _ = await _seed_asset(other_id, with_object=True)
    await _seed_session(other_id, other_asset)
    other_rows = {
        table: await _count(table, other_id)
        for table in ("edit_sessions", "edit_history", "assets", "tool_runs", "agent_runs")
    }
    other_keys = await asyncio.to_thread(_prefix_keys, other_id)

    operation_id = await _full_delete_flow(
        admin_client, user_id, other_credentials["username"]
    )
    result = await _drive_cleanup(operation_id)

    assert _value(result["status"]) == "succeeded", result
    assert result["failure_code"] is None, result
    assert result["finished_at"] is not None, result
    # 全部在线业务行清零（RESTRICT 约束下任何残留都会使 business_rows 事务失败）；
    # 墙行（session_assets 无 user_id 列）经会话归属关联断言
    for table in ("edit_sessions", "edit_history", "assets", "tool_runs", "agent_runs"):
        assert await _count(table, user_id) == 0, table
    assert await _wall_count(user_id) == 0
    # 墓碑删除
    assert await _scalar("SELECT count(*) FROM users WHERE id = :u", u=user_id) == 0
    # 前缀清空（含孤儿）
    assert await asyncio.to_thread(_prefix_keys, user_id) == []
    # 其他用户零影响
    for table, before in other_rows.items():
        assert await _count(table, other_id) == before, table
    assert await asyncio.to_thread(_prefix_keys, other_id) == other_keys
    assert await _scalar("SELECT count(*) FROM users WHERE id = :u", u=other_id) == 1


async def test_cleanup_clears_exact_prefix_and_spares_neighbors(
    admin_client, credentials, deletion_enabled
):
    """矩阵行 3（ADM-FR-013 / ADM-AC-027）：精确前缀（末尾斜杠）清理——目标前缀
    孤儿清空；相邻前缀（缺尾斜杠即误伤的两种键形）与其他用户前缀不受扰。"""
    await _make_admin(admin_client, credentials)
    user_id, username = await _insert_user()
    await storage.put(
        f"users/{user_id}/orphan-{uuid.uuid4().hex}.bin", b"o", "application/octet-stream"
    )
    # 两种「相邻键」：目录形相邻前缀 + 直接以 uuid 结尾拼后缀的对象
    neighbor_dir = f"users/{user_id}neighbor/x.bin"
    neighbor_tail = f"users/{user_id}tail.bin"
    await storage.put(neighbor_dir, b"n", "application/octet-stream")
    await storage.put(neighbor_tail, b"n", "application/octet-stream")
    # 其他用户的对象
    other_id, _ = await _insert_user()
    other_key = f"users/{other_id}/keep-{uuid.uuid4().hex}.bin"
    await storage.put(other_key, b"k", "application/octet-stream")
    try:
        operation_id = await _full_delete_flow(admin_client, user_id, username)
        result = await _drive_cleanup(operation_id)

        assert _value(result["status"]) == "succeeded", result
        assert await asyncio.to_thread(_prefix_keys, user_id) == []
        assert await _scalar("SELECT count(*) FROM users WHERE id = :u", u=user_id) == 0
        # 相邻键形不在目标前缀内——用对象可读性断言其存活
        assert await storage.get(neighbor_dir) == b"n"
        assert await storage.get(neighbor_tail) == b"n"
        assert await storage.get(other_key) == b"k"
        assert await _scalar("SELECT count(*) FROM users WHERE id = :u", u=other_id) == 1
    finally:
        for key in (neighbor_dir, neighbor_tail, other_key):
            await storage.delete(key)  # 测试自清（不在目标前缀内，夹具扫不到）


async def test_cleanup_removes_versions_and_delete_markers_in_versioned_bucket(
    admin_client, credentials, monkeypatch, deletion_enabled
):
    """矩阵行 3（ADM-FR-013 / ADM-AC-027 版本化半边）：启用版本的目标桶上，
    同 key 多版本与删除标记必须连版本一起清空，仅清当前版本不放行完成。

    环境说明：对开发 MinIO 测试桶临时 put_bucket_versioning(Enabled)，finally 恢复
    Suspended 并由 autouse 夹具按版本自清本用例产生的对象；桶全局状态仅存在于
    本用例窗口内（pytest 串行执行）。
    """
    client = s3_client_factory()
    bucket = _bucket()
    await asyncio.to_thread(
        client.put_bucket_versioning,
        Bucket=bucket,
        VersioningConfiguration={"Status": "Enabled"},
    )
    try:
        await _make_admin(admin_client, credentials)
        user_id, username = await _insert_user()
        versioned_key = f"users/{user_id}/versioned-{uuid.uuid4().hex}.bin"
        marker_key = f"users/{user_id}/marked-{uuid.uuid4().hex}.bin"
        await storage.put(versioned_key, b"v1", "application/octet-stream")
        await storage.put(versioned_key, b"v2", "application/octet-stream")
        await storage.put(marker_key, b"m", "application/octet-stream")
        await storage.delete(marker_key)  # 版本化桶上产生删除标记

        versions, markers = await asyncio.to_thread(_prefix_versions, user_id)
        assert len(versions) == 3 and len(markers) == 1  # 造数前提：版本与标记真实存在

        operation_id = await _full_delete_flow(admin_client, user_id, username)
        result = await _drive_cleanup(operation_id)

        assert _value(result["status"]) == "succeeded", result
        assert await asyncio.to_thread(_prefix_versions, user_id) == ([], [])
        assert await _scalar("SELECT count(*) FROM users WHERE id = :u", u=user_id) == 0
    finally:
        await asyncio.to_thread(
            client.put_bucket_versioning,
            Bucket=bucket,
            VersioningConfiguration={"Status": "Suspended"},
        )


# ===========================================================================
# 矩阵行 4（ADM-FR-014 / ADM-AC-028、029）：写入屏障与迟到 Worker（故障注入）
# ===========================================================================


async def test_barrier_rejects_new_writes_once_frozen():
    """矩阵行 4（ADM-FR-014 / ADM-AC-028）：冻结后的新对象写入在登记入口被拒——
    不落登记行、不 PUT 对象、不产生素材行（WI 决策 2a）。"""
    from app.services import assets as asset_service

    user_id, _ = await _insert_user()
    await _freeze_user(user_id)

    with pytest.raises(Exception):
        async with SessionFactory() as session:
            await asset_service.create_from_bytes(
                session, user_id, make_image((64, 64)),
                AssetKind.GENERATED, AssetSource.GENERATE,
            )

    assert await _count("assets", user_id) == 0
    assert await asyncio.to_thread(_prefix_keys, user_id) == []
    assert await _pending_states(user_id) == []  # deleting 状态不得登记（决策 2a）


async def test_barrier_compensates_write_registered_before_freeze(monkeypatch):
    """矩阵行 4（ADM-FR-014 / ADM-AC-028，暂停点=登记后）：登记完成后、PUT 前
    冻结——写路径在锁内重读用户状态被拒，已登记写入锁外补偿（删对象 + 置
    aborted），素材行零新增（WI 决策 2c）。"""
    from app.services import assets as asset_service

    user_id, _ = await _insert_user()
    real_put = storage.put

    async def freeze_then_put(key: str, data: bytes, content_type: str) -> None:
        await _freeze_user(user_id)  # 登记已提交，此刻冻结
        await real_put(key, data, content_type)

    monkeypatch.setattr("app.storage.put", freeze_then_put)

    with pytest.raises(Exception):
        async with SessionFactory() as session:
            await asset_service.create_from_bytes(
                session, user_id, make_image((64, 64)),
                AssetKind.GENERATED, AssetSource.GENERATE,
            )

    assert await _count("assets", user_id) == 0
    assert await _pending_states(user_id) == ["aborted"]  # 补偿必须落 aborted（决策 2c）
    assert await asyncio.to_thread(_prefix_keys, user_id) == []  # 已 PUT 的对象被补偿删除


async def test_barrier_compensates_write_uploaded_before_freeze(monkeypatch):
    """矩阵行 4（ADM-FR-014 / ADM-AC-028，暂停点=PUT 后）：对象已上传、落库前
    冻结——同样拒绝提交并补偿清理对象与登记行。"""
    from app.services import assets as asset_service

    user_id, _ = await _insert_user()
    real_put = storage.put

    async def put_then_freeze(key: str, data: bytes, content_type: str) -> None:
        await real_put(key, data, content_type)
        await _freeze_user(user_id)  # PUT 已完成、锁未取，此刻冻结

    monkeypatch.setattr("app.storage.put", put_then_freeze)

    with pytest.raises(Exception):
        async with SessionFactory() as session:
            await asset_service.create_from_bytes(
                session, user_id, make_image((64, 64)),
                AssetKind.GENERATED, AssetSource.GENERATE,
            )

    assert await _count("assets", user_id) == 0
    assert await _pending_states(user_id) == ["aborted"]
    assert await asyncio.to_thread(_prefix_keys, user_id) == []


async def test_cleanup_absorbs_write_committed_just_before_freeze(monkeypatch):
    """矩阵行 4（ADM-FR-014 / ADM-AC-028，暂停点=提交前）+ succeeded 后无新文件。

    生产中「校验后、提交前」的冻结由用户锁串行化（确认流程拿不到锁），测试以
    SQL 绕锁注入等价时序：屏障校验已过、素材行与 committed 登记行随提交落库、
    对象已存在——清理必须把这份迟到提交一并吸收（business_rows 删行、objects
    删对象、finalize 放行），且 succeeded 后再写被拒、前缀保持为空。
    """
    from app.services import assets as asset_service
    from app.services import guards

    await _require_deletion_tables()  # 本用例需直插操作行（冻结以 SQL 注入）
    user_id, _ = await _insert_user()
    planted: list[uuid.UUID] = []
    real_check = guards.assert_writable

    async def check_then_freeze(session):
        await real_check(session)  # 此时状态仍 active，校验放行
        await _freeze_user(user_id)  # 校验后、提交前的冻结（SQL 绕锁注入）
        planted.append(await _plant_operation(user_id))

    monkeypatch.setattr("app.services.guards.assert_writable", check_then_freeze)

    async with SessionFactory() as session:
        await asset_service.create_from_bytes(
            session, user_id, make_image((64, 64)),
            AssetKind.GENERATED, AssetSource.GENERATE,
        )
    assert planted, "冻结与操作行必须已由提交前窗口注入"
    assert await _count("assets", user_id) == 1  # 迟到提交已落库
    assert await _pending_states(user_id) == ["committed"]

    result = await _drive_cleanup(planted[0])

    assert _value(result["status"]) == "succeeded", result
    assert await _count("assets", user_id) == 0
    assert await _scalar("SELECT count(*) FROM users WHERE id = :u", u=user_id) == 0
    assert await asyncio.to_thread(_prefix_keys, user_id) == []
    assert all(state != "registered" for state in await _pending_states(user_id))

    # succeeded 后再写被拒（用户行已删，登记入口拒绝），前缀不出现新文件
    with pytest.raises(Exception):
        async with SessionFactory() as session:
            await asset_service.create_from_bytes(
                session, user_id, make_image((64, 64)),
                AssetKind.GENERATED, AssetSource.GENERATE,
            )
    assert await asyncio.to_thread(_prefix_keys, user_id) == []


async def test_late_worker_cannot_rebuild_rows_after_cleanup_succeeded(
    admin_client, client, credentials, other_credentials, monkeypatch, deletion_enabled
):
    """矩阵行 4（ADM-FR-014 / ADM-AC-029）：已终态的旧 Worker 在清理完成后迟到
    返回——不得重建素材/任务行、不得在前缀留下新文件；放行依据是账号生命周期
    与写入屏障，而非任务终态本身（AC-029）。手法与 test_deletion_concurrency 的
    迟到 Worker 用例一致（替换 handler + 直调 run_tool）。"""
    from app.services import assets as asset_service
    from app.services import runs as runs_service
    from app.tasks.tools import run_tool

    _set_switch(monkeypatch, True)
    await _make_admin(admin_client, credentials)
    assert (await client.post("/api/auth/register", json=other_credentials)).status_code == 201
    user_id = await _track_client_user(client)
    session = await open_session(client)

    canceled = asyncio.Event()
    release = asyncio.Event()

    async def late_handler(db, run):
        # 暂停点：模拟外部模型调用悬挂；恢复后先把 run 打成终态（删除门禁放行的前提）
        async with SessionFactory() as other:
            twin = await other.get(ToolRun, run.id)
            await runs_service.finish(other, twin, status=RunStatus.CANCELED, error="让路删除")
        canceled.set()
        await release.wait()
        # 迟到的旧执行在清理完成后写入产物：必须被屏障/代次拒绝
        await asset_service.create_from_bytes(
            db, run.user_id, make_image((64, 64)),
            AssetKind.GENERATED, AssetSource.GENERATE,
        )
        return {"asset_ids": []}

    _patch_adjust_handler(monkeypatch, late_handler)
    body = await invoke(client, session["id"], "adjust_image", {"brightness": 0.1})
    worker = asyncio.create_task(run_tool({}, uuid.UUID(body["run"]["id"])))
    await canceled.wait()  # run 已终态，删除门禁可放行

    operation_id = await _full_delete_flow(
        admin_client, user_id, other_credentials["username"]
    )
    result = await _drive_cleanup(operation_id)
    assert _value(result["status"]) == "succeeded", result

    release.set()
    await worker  # 迟到执行被拒后 run_tool 正常收尾（既有迟到 Worker 用例同款语义）

    assert await _scalar("SELECT count(*) FROM users WHERE id = :u", u=user_id) == 0
    assert await _count("assets", user_id) == 0, "迟到 Worker 不得重建素材"
    assert await _count("tool_runs", user_id) == 0, "迟到 Worker 不得复活任务行"
    assert await _count("agent_runs", user_id) == 0
    assert await asyncio.to_thread(_prefix_keys, user_id) == [], "succeeded 后不得出现新文件"
    assert _value((await _op_row(operation_id))["status"]) == "succeeded"


# ===========================================================================
# 矩阵行 5（ADM-FR-015 / ADM-AC-030～033）：持久化、失败与恢复（故障注入）
# ===========================================================================


async def test_s3_delete_failure_marks_failed_then_retry_completes(
    admin_client, client, credentials, other_credentials, monkeypatch, deletion_enabled
):
    """矩阵行 5（ADM-FR-015 / ADM-AC-030）：S3 删除失败 → failed（不误报空、
    不撤销冻结、不转回正常账号）；重试原 operation_id 续跑可完成，缺失对象按
    已处理；deletion_retry 留痕含 operation_id（矩阵行 7）。"""
    await _make_admin(admin_client, credentials)
    assert (await client.post("/api/auth/register", json=other_credentials)).status_code == 201
    user_id = await _track_client_user(client)
    await open_session(client)
    orphan_key = f"users/{user_id}/orphan-{uuid.uuid4().hex}.bin"
    await storage.put(orphan_key, b"o", "application/octet-stream")

    real_client = s3_client_factory()

    class BrokenDelete:
        def __getattr__(self, name):
            return getattr(real_client, name)

        def delete_object(self, **kwargs):
            raise RuntimeError("s3 delete down (injected)")

        def delete_objects(self, **kwargs):
            raise RuntimeError("s3 delete down (injected)")

    monkeypatch.setattr("app.storage._client", lambda *args, **kwargs: BrokenDelete())

    operation_id = await _full_delete_flow(
        admin_client, user_id, other_credentials["username"]
    )
    result = await _drive_cleanup(operation_id)

    assert _value(result["status"]) == "failed", result  # 不得伪报完成
    assert result["failure_code"], result  # 脱敏失败码可定位
    assert result["finished_at"] is None, result
    row = await _user_row(user_id)
    assert _value(row["status"]) == "deleting"  # 不撤销冻结、不转回正常账号
    assert await asyncio.to_thread(_prefix_keys, user_id), "不误报前缀为空"
    user_delete_rows = await _audit_rows_for_operation(operation_id)
    assert any(row["action"] == "user_delete" for row in user_delete_rows)

    # 重试（原 ID）：解除注入并手工清走一个对象，验证缺失对象按已处理
    monkeypatch.undo()  # 同时还原开关补丁：retry 本就不受开关限制（矩阵行 8）
    await storage.delete(orphan_key)
    retry = await admin_client.post(f"/api/admin/deletion-operations/{operation_id}/retry")
    assert retry.status_code == 202, retry.text
    assert retry.json()["id"] == str(operation_id)  # 原 ID

    result = await _drive_cleanup(operation_id)
    assert _value(result["status"]) == "succeeded", result
    assert await asyncio.to_thread(_prefix_keys, user_id) == []
    assert await _scalar("SELECT count(*) FROM users WHERE id = :u", u=user_id) == 0
    retry_rows = await _audit_rows_for_operation(operation_id)
    assert any(row["action"] == "deletion_retry" for row in retry_rows), retry_rows
    assert all(row["action"] in ("user_delete", "deletion_retry") for row in retry_rows)


async def test_db_failure_in_business_rows_marks_failed_then_retry_completes(
    admin_client, client, credentials, other_credentials, deletion_enabled
):
    """矩阵行 5（ADM-FR-015 / ADM-AC-031）：business_rows 阶段 DB 故障（守表
    RESTRICT 注入，test_deletion_safety 同法）→ failed、账号保持冻结、外部事实
    不被伪装回滚；解除后按原 ID 续跑可完成。"""
    await _make_admin(admin_client, credentials)
    assert (await client.post("/api/auth/register", json=other_credentials)).status_code == 201
    user_id = await _track_client_user(client)
    session = await open_session(client)
    protected = uuid.UUID(session["current_asset_id"])

    preview = await admin_client.get(f"/api/admin/users/{user_id}/deletion-preview")
    assert preview.status_code == 200, preview.text
    token = preview.json()["preview_token"]

    guard = f"_adm_del_guard_{uuid.uuid4().hex[:8]}"
    try:
        async with SessionFactory() as db:
            await db.execute(
                text(
                    f"CREATE TABLE {guard} "
                    "(asset_id uuid NOT NULL REFERENCES assets(id) ON DELETE RESTRICT)"
                )
            )
            await db.execute(
                text(f"INSERT INTO {guard} (asset_id) VALUES (:a)"), {"a": protected}
            )
            await db.commit()

        response = await admin_client.request("DELETE", 
            f"/api/admin/users/{user_id}",
            headers={"X-Deletion-Preview": token},
            json={"confirm_username": other_credentials["username"]},
        )
        assert response.status_code == 202, response.text
        operation_id = uuid.UUID(response.json()["id"])
        result = await _drive_cleanup(operation_id)
    finally:
        async with SessionFactory() as db:
            await db.execute(text(f"DROP TABLE IF EXISTS {guard}"))
            await db.commit()

    assert _value(result["status"]) == "failed", result
    assert result["failure_code"], result
    row = await _user_row(user_id)
    assert _value(row["status"]) == "deleting"  # 账号不重新开放
    assert await _count("assets", user_id) > 0  # 业务行清理未完成，不得伪报

    retry = await admin_client.post(f"/api/admin/deletion-operations/{operation_id}/retry")
    assert retry.status_code == 202, retry.text
    result = await _drive_cleanup(operation_id)
    assert _value(result["status"]) == "succeeded", result
    for table in ("edit_sessions", "edit_history", "assets", "tool_runs", "agent_runs"):
        assert await _count(table, user_id) == 0, table
    assert await _wall_count(user_id) == 0
    assert await _scalar("SELECT count(*) FROM users WHERE id = :u", u=user_id) == 0


async def test_recovery_scan_redispatches_lost_operation_idempotently(
    admin_client, client, credentials, other_credentials, deletion_enabled
):
    """矩阵行 5（ADM-FR-015 / ADM-AC-032）：投递丢失（确认已持久化、无人消费）→
    recover_deletions 恢复派发，同 operation_id 幂等；重复执行清理不产生并发副本、
    不重复删除、审计链不重复。投递副作用（ARQ 队列内容）不可直接观测，以恢复后
    清理可续跑且终态唯一为可观测契约（诚实变体，见模块 docstring）。"""
    await _make_admin(admin_client, credentials)
    assert (await client.post("/api/auth/register", json=other_credentials)).status_code == 201
    user_id = await _track_client_user(client)
    await open_session(client)

    operation_id = await _full_delete_flow(
        admin_client, user_id, other_credentials["username"]
    )
    assert _value((await _op_row(operation_id))["status"]) == "processing"  # 无人消费

    from app.tasks.deletion import cleanup_account, recover_deletions  # 惰性导入：Red 先行

    await recover_deletions({})  # 恢复扫描：不得报错、不得复制操作
    assert await _op_count(user_id) == 1
    result = await _drive_cleanup(operation_id)
    assert _value(result["status"]) == "succeeded", result
    assert await _scalar("SELECT count(*) FROM users WHERE id = :u", u=user_id) == 0
    assert await asyncio.to_thread(_prefix_keys, user_id) == []

    # 重复消息/重复调用：对已 succeeded 的操作再执行幂等返回，不重删、审计链不重复
    await recover_deletions({})
    await cleanup_account({}, operation_id)
    assert _value((await _op_row(operation_id))["status"]) == "succeeded"
    rows = await _audit_rows_for_operation(operation_id)
    assert len([row for row in rows if row["action"] == "user_delete"]) == 1
    assert await asyncio.to_thread(_prefix_keys, user_id) == []


async def test_unresolved_pending_write_blocks_completion_until_resolved(
    admin_client, client, credentials, other_credentials, deletion_enabled
):
    """矩阵行 5（ADM-FR-015 / ADM-AC-033）：存在未了结的登记写入（registered、
    未超时）时，双重核对不过不得 succeeded（finished_at 为空、账号保持冻结）；
    写入方补偿（aborted）后清理方可完成。"""
    await _make_admin(admin_client, credentials)
    assert (await client.post("/api/auth/register", json=other_credentials)).status_code == 201
    user_id = await _track_client_user(client)
    await open_session(client)

    operation_id = await _full_delete_flow(
        admin_client, user_id, other_credentials["username"]
    )
    await _plant_pending_write(user_id, f"users/{user_id}/in-flight.bin", age_seconds=0)

    from app.tasks.deletion import cleanup_account  # 惰性导入：Red 先行

    for _ in range(3):
        await cleanup_account({}, operation_id)
    result = await _op_row(operation_id)
    assert _value(result["status"]) == "processing", result  # 双重核对不过不 succeeded
    assert result["finished_at"] is None, result
    row = await _user_row(user_id)
    assert _value(row["status"]) == "deleting"  # 墓碑保留以阻断新写

    # 写入方自了结（等价于屏障补偿路径）：登记行转 aborted
    async with SessionFactory() as session:
        await session.execute(
            text("UPDATE pending_object_writes SET state = 'aborted' WHERE user_id = :u"),
            {"u": user_id},
        )
        await session.commit()

    result = await _drive_cleanup(operation_id)
    assert _value(result["status"]) == "succeeded", result
    assert await _scalar("SELECT count(*) FROM users WHERE id = :u", u=user_id) == 0
    assert await asyncio.to_thread(_prefix_keys, user_id) == []


async def test_recovery_scan_compensates_crashed_registration(
    admin_client, client, credentials, other_credentials, deletion_enabled
):
    """矩阵行 5（ADM-FR-015 / ADM-AC-032、033 延伸）：崩溃残留的登记行（registered
    且超时）由恢复扫描回收——补偿删对象 + 置 aborted（WI 决策 4d），清理随后完成。"""
    await _make_admin(admin_client, credentials)
    assert (await client.post("/api/auth/register", json=other_credentials)).status_code == 201
    user_id = await _track_client_user(client)
    await open_session(client)

    operation_id = await _full_delete_flow(
        admin_client, user_id, other_credentials["username"]
    )
    crashed_key = f"users/{user_id}/crashed-{uuid.uuid4().hex}.bin"
    await storage.put(crashed_key, b"crash", "application/octet-stream")
    await _plant_pending_write(user_id, crashed_key, age_seconds=2 * 3600)

    from app.tasks.deletion import recover_deletions  # 惰性导入：Red 先行

    await recover_deletions({})

    states = await _pending_states(user_id)
    assert "registered" not in states, states  # 残留登记行已被回收
    assert await asyncio.to_thread(_prefix_keys, user_id) == [], "补偿必须删除残留对象"

    result = await _drive_cleanup(operation_id)
    assert _value(result["status"]) == "succeeded", result
    assert await _scalar("SELECT count(*) FROM users WHERE id = :u", u=user_id) == 0


# ===========================================================================
# 矩阵行 6/7（ADM-FR-016 / ADM-AC-034；FR-023 扩展）：审计清除与最小保留
# ===========================================================================


async def test_cleanup_clears_prior_target_audit_keeps_minimal_chain(
    admin_client, credentials, deletion_enabled
):
    """矩阵行 6/7（ADM-FR-016 / ADM-AC-034 + FR-023）：删除完成后目标此前的
    管理记录被清除，仅保留本次删除链（user_delete，含 operation_id）最小记录；
    summary 不含用户名、对象 key 清单等业务内容。"""
    admin = await _make_admin(admin_client, credentials)
    user_id, original_name = await _insert_user()

    # 目标此前的管理记录：改名/改密/停用/启用各一条（全部 target=该用户）
    renamed_name = f"test_prev_{uuid.uuid4().hex[:8]}"
    assert (
        await admin_client.patch(
            f"/api/admin/users/{user_id}", json={"username": renamed_name}
        )
    ).status_code == 200
    assert (
        await admin_client.post(
            f"/api/admin/users/{user_id}/reset-password", json={"password": "fresh-pass-1"}
        )
    ).status_code == 204
    assert (
        await admin_client.patch(f"/api/admin/users/{user_id}", json={"status": "disabled"})
    ).status_code == 200
    assert (
        await admin_client.patch(f"/api/admin/users/{user_id}", json={"status": "active"})
    ).status_code == 200
    prior = await _audit_rows_for_target(user_id)
    assert {row["action"] for row in prior} == {
        "user_rename", "user_reset_password", "user_disable", "user_enable"
    }  # 造数前提

    asset_id, asset_key = await _seed_asset(user_id, with_object=True)
    operation_id = await _full_delete_flow(admin_client, user_id, renamed_name)
    result = await _drive_cleanup(operation_id)
    assert _value(result["status"]) == "succeeded", result

    rows = await _audit_rows_for_target(user_id)
    assert rows, "本次删除链必须保留可追踪记录"
    assert {row["action"] for row in rows} == {"user_delete"}, rows  # 旧记录全部清除
    assert all(row["operation_id"] == str(operation_id) for row in rows)  # FR-023
    assert all(row["result"] == "success" for row in rows)
    for row in rows:
        # 最小化保留：无用户名（改名前/后）、无对象 key/清单、无业务摘要、无密码痕迹
        assert original_name not in row["summary"], row
        assert renamed_name not in row["summary"], row
        assert "users/" not in row["summary"], row
        assert asset_key not in row["summary"], row
        assert str(asset_id) not in row["summary"], row
        assert "fresh-pass-1" not in row["summary"], row
    # 其他目标的记录不受清除波及（按操作者维度抽查管理员自身痕迹仍在）
    remaining = await _scalar(
        "SELECT count(*) FROM admin_audit_logs WHERE actor_id = :a",
        a=uuid.UUID(admin["id"]),
    )
    assert remaining >= len(rows)


# ===========================================================================
# 矩阵行 8（FR-027）：清理开关
# ===========================================================================


async def test_switch_off_blocks_preview_and_delete_but_retry_still_works(
    admin_client, credentials, monkeypatch
):
    """矩阵行 8（FR-027 / ADM-AC-053 后半）：开关关闭 → preview/DELETE 均
    503 deletion_unavailable、零写入零审计；操作详情读取与 retry 不受开关限制，
    已接受的操作仍可走完清理。"""
    _set_switch(monkeypatch, False)  # 显式关闭（默认即 False；实现前 no-op）
    admin = await _make_admin(admin_client, credentials)
    user_id, username = await _insert_user()
    await storage.put(
        f"users/{user_id}/switch-off-{uuid.uuid4().hex}.bin", b"x", "application/octet-stream"
    )
    audit_before = await _scalar(
        "SELECT count(*) FROM admin_audit_logs WHERE actor_id = :a", a=uuid.UUID(admin["id"])
    )

    preview = await admin_client.get(f"/api/admin/users/{user_id}/deletion-preview")
    assert preview.status_code == 503, preview.text
    detail = preview.json()["detail"]
    assert isinstance(detail, dict), detail
    assert detail["code"] == "deletion_unavailable"

    delete = await admin_client.request("DELETE", 
        f"/api/admin/users/{user_id}",
        headers={"X-Deletion-Preview": "whatever"},
        json={"confirm_username": username},
    )
    assert delete.status_code == 503, delete.text
    assert delete.json()["detail"]["code"] == "deletion_unavailable"

    # 零写入零审计
    assert (await _user_row(user_id))["status"] == "active"
    assert await asyncio.to_thread(_prefix_keys, user_id), "对象不得被清理"
    assert await _scalar(
        "SELECT count(*) FROM admin_audit_logs WHERE actor_id = :a", a=uuid.UUID(admin["id"])
    ) == audit_before
    if await _scalar("SELECT to_regclass('account_deletion_operations') IS NOT NULL"):
        assert await _op_count(user_id) == 0

    # retry 不受开关限制：先在开关开启时制造一个 failed 操作
    _set_switch(monkeypatch, True)
    real_client = s3_client_factory()

    class BrokenDelete:
        def __getattr__(self, name):
            return getattr(real_client, name)

        def delete_object(self, **kwargs):
            raise RuntimeError("s3 delete down (injected)")

        def delete_objects(self, **kwargs):
            raise RuntimeError("s3 delete down (injected)")

    monkeypatch.setattr("app.storage._client", lambda *args, **kwargs: BrokenDelete())
    operation_id = await _full_delete_flow(admin_client, user_id, username)
    assert _value((await _drive_cleanup(operation_id))["status"]) == "failed"
    monkeypatch.undo()  # 解除 S3 注入；开关补丁同时还原（retry 本就不受开关限制）

    _set_switch(monkeypatch, False)
    detail_response = await admin_client.get(f"/api/admin/deletion-operations/{operation_id}")
    assert detail_response.status_code == 200, detail_response.text  # 读操作可用
    retry = await admin_client.post(f"/api/admin/deletion-operations/{operation_id}/retry")
    assert retry.status_code == 202, retry.text  # retry 不受开关限制

    result = await _drive_cleanup(operation_id)
    assert _value(result["status"]) == "succeeded", result
    assert await _scalar("SELECT count(*) FROM users WHERE id = :u", u=user_id) == 0
    assert await asyncio.to_thread(_prefix_keys, user_id) == []
