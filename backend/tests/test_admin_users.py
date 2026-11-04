"""WI-20261025-02 用户维护与审计专项测试（Red 阶段）。

对应测试矩阵 10 行（docs/work-items/WI-20261025-02-admin-users-audit.md），
每条用例 docstring 标注所属矩阵行与验收场景。Red 与造数约定：

- 阶段 2 新端点（``/api/admin/users`` 全套）尚不存在：请求落入 ``main.py`` 的
  ``/api`` 兜底路由得到 404，每条用例在第一个涉及新端点的断言上以具体断言失败
  呈现 Red；本文件无需按名导入任何尚不存在模块（routers/admin、services/audit、
  services/paging、AdminAuditLog），文件天然可收集，不存在收集错误；
- AdminAuditLog 尚无模型：审计断言全部走 SQLAlchemy ``text()`` 直查
  ``admin_audit_logs``，与阶段 1 对新列「直改直查」的处理方式一致；
- 管理员造数沿用阶段 1 ``test_admin_auth.py`` 的先例（require_admin 三分支
  用例）：注册 ``test_`` 账号后 SQL 直改 ``role='admin'``，不依赖尚不存在的
  管理端点；
- 开发库存在非 ``test_`` 前缀的真实账号（手工冒烟遗留）：凡涉及列表 total、
  排序、筛选的断言，一律以 WI 固化契约的同口径 SQL 为预期基准（同过滤条件、
  同排序键、id 同方向稳定次序），不假设库内只有测试数据。
"""

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport
from langchain_core.messages import AIMessage
from sqlalchemy import text

from app.agent import graph
from app.db import SessionFactory
from app.main import app
from app.models import AgentRun
from app.models.tool_run import RunStatus
from app.security import SESSION_COOKIE
from app.services import runs
from app.tasks.tools import run_tool
from tests.test_agent import FakePlanner, send, tool_calls
from tests.test_assets import create_mask, open_session, upload_asset


@pytest.fixture
def fake_planner(monkeypatch):
    """与 tests/test_agent.py 相同的假规划器安装器（本地重声明，避免导入名与用例参数同名）。

    按调用次序回放 AIMessage；多步计划经 needs_confirm 停在 queued 等待确认。
    """

    def install(message: AIMessage, *more: AIMessage) -> FakePlanner:
        fake = FakePlanner(message, *more)
        monkeypatch.setattr(graph, "planner", lambda: fake)
        return fake

    return install


def _value(raw):
    """text() 直查返回原始字符串；实现后经枚举列映射读出的是枚举成员，统一还原为值。"""
    return getattr(raw, "value", raw)


async def _scalar(sql: str, **params):
    async with SessionFactory() as session:
        return (await session.execute(text(sql), params)).scalar_one()


async def _require_no_external_admin() -> None:
    """与 tests/test_admin_auth.py 同一前置（矩阵行约定）：测试只增删 test_ 前缀账号。

    若开发库已有非测试管理员（如手工跑过 init_admin），本文件涉及提升管理员的
    用例无法如实运行——直接以明确的失败说明环境前置条件，绝不改写非测试数据。
    """
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
    """注册 test_ 账号并 SQL 直提为管理员，返回 {"id", "username", "password"}。

    提升前清掉可能残留的 test_ 前缀管理员（与 conftest 清理同边界，只动 test_
    数据），避免上个崩溃会话的残留触发唯一管理员索引冲突。
    """
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


async def _insert_user(
    *, username: str | None = None, created_at: datetime | None = None, status: str = "active"
) -> tuple[str, str]:
    """SQL 直插 test_ 账号（绕过注册端点的自动登录），返回 (id, username)。"""
    user_id = uuid.uuid4()
    username = username or f"test_{uuid.uuid4().hex[:12]}"
    params: dict = {"id": user_id, "username": username, "password_hash": "x"}
    columns = "id, username, password_hash"
    values = ":id, :username, :password_hash"
    if created_at is not None:
        columns += ", created_at"
        values += ", :created_at"
        params["created_at"] = created_at
    if status != "active":
        columns += ", status"
        values += ", :status"
        params["status"] = status
    async with SessionFactory() as session:
        await session.execute(text(f"INSERT INTO users ({columns}) VALUES ({values})"), params)
        await session.commit()
    return str(user_id), username


async def _ordered_ids(order: str, where: str = "TRUE", **params) -> list[str]:
    """WI 决策 3/4 的契约口径：created_at 主排序、id 同方向稳定次序，可叠加过滤。

    这是断言的预期基准（oracle）：由 WI 文本转写，而非读取实现。
    """
    assert order in ("asc", "desc")
    async with SessionFactory() as session:
        rows = await session.execute(
            text(
                f"SELECT id::text FROM users WHERE {where}"
                f" ORDER BY created_at {order}, id {order}"
            ),
            params,
        )
        return rows.scalars().all()


async def _fetch_user_row(user_id: str) -> dict:
    async with SessionFactory() as session:
        row = await session.execute(
            text(
                "SELECT username, role::text, status::text, token_version"
                " FROM users WHERE id = :u"
            ),
            {"u": uuid.UUID(user_id)},
        )
        return row.mappings().one()


async def _audit_rows(actor_id: str, target_id: str | None = None) -> list[dict]:
    """text() 直查审计表；实现前表不存在，首个触点即为该用例的具体失败。"""
    sql = (
        "SELECT actor_id::text AS actor_id, target_type, target_id::text AS target_id,"
        " action, result, COALESCE(summary::text, '') AS summary"
        " FROM admin_audit_logs WHERE actor_id = :actor"
    )
    params: dict = {"actor": actor_id}
    if target_id is not None:
        sql += " AND target_id = :target"
        params["target"] = target_id
    sql += " ORDER BY created_at, id"
    async with SessionFactory() as session:
        rows = await session.execute(text(sql), params)
        return [dict(row._mapping) for row in rows]


async def _audit_count(actor_id: str) -> int:
    async with SessionFactory() as session:
        return await session.scalar(
            text("SELECT count(*) FROM admin_audit_logs WHERE actor_id = :a"),
            {"a": actor_id},
        )


async def _turn_state(turn_id: str) -> tuple[str, list]:
    async with SessionFactory() as session:
        row = (
            await session.execute(
                text("SELECT status, plan FROM agent_runs WHERE id = :t"),
                {"t": uuid.UUID(turn_id)},
            )
        ).one()
        return row.status, row.plan


async def _insert_hidden_asset(user_id: str, *, size_bytes: int) -> None:
    """直插一条 library_hidden 素材（创意库不可见，但统计必须计入）。"""
    async with SessionFactory() as session:
        await session.execute(
            text(
                "INSERT INTO assets (id, user_id, kind, source, storage_key,"
                " image_format, width, height, size_bytes, has_alpha, library_hidden)"
                " VALUES (:id, :user_id, 'original', 'upload', :key, 'PNG', 64, 64,"
                " :size, false, true)"
            ),
            {
                "id": uuid.uuid4(),
                "user_id": uuid.UUID(user_id),
                "key": f"test/admin-stats/{uuid.uuid4()}",
                "size": size_bytes,
            },
        )
        await session.commit()


async def _insert_agent_run(user_id: str, session_id: str, revision: int) -> None:
    """直插一条 AgentRun（对话轮次），与 ToolRun 分开计数。"""
    async with SessionFactory() as session:
        session.add(
            AgentRun(
                user_id=uuid.UUID(user_id),
                session_id=uuid.UUID(session_id),
                revision=revision,
                goal="测试轮次",
                plan=[],
                status="succeeded",
            )
        )
        await session.commit()


@pytest.fixture
async def admin_client():
    """独立于目标用户 ``client`` 的第二支客户端：承载管理员会话，互不串 Cookie。"""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---- 矩阵行 1（ADM-FR-004 / WI-AC-001）：分页、稳定排序与 422 边界 ----


async def test_user_list_pagination_slices_pages(admin_client, credentials):
    """page/page_size 切片正确，返回 items+total+page+page_size，total 为真实总数。"""
    await _make_admin(admin_client, credentials)
    for _ in range(3):
        await _insert_user()
    expected = await _ordered_ids("desc")  # 显式 sort=created_at&order=desc 的契约口径

    response = await admin_client.get(
        "/api/admin/users",
        params={"sort": "created_at", "order": "desc", "page": 1, "page_size": 2},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == len(expected)
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert [item["id"] for item in body["items"]] == expected[:2]

    second = (
        await admin_client.get(
            "/api/admin/users",
            params={"sort": "created_at", "order": "desc", "page": 2, "page_size": 2},
        )
    ).json()
    assert [item["id"] for item in second["items"]] == expected[2:4]

    # 合法上界 page_size=100 一页取全
    full = (
        await admin_client.get(
            "/api/admin/users",
            params={"sort": "created_at", "order": "desc", "page": 1, "page_size": 100},
        )
    ).json()
    assert [item["id"] for item in full["items"]] == expected


async def test_user_list_out_of_range_page_returns_empty_items_with_real_total(
    admin_client, credentials
):
    """越界页返回空 items 与真实 total，不报错也不复用末页。"""
    await _make_admin(admin_client, credentials)
    expected = await _ordered_ids("desc")

    response = await admin_client.get(
        "/api/admin/users", params={"sort": "created_at", "order": "desc", "page": 999}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["total"] == len(expected)
    assert body["page"] == 999


@pytest.mark.parametrize(
    "params",
    [{"page": 0}, {"page_size": 0}, {"page_size": 101}],
    ids=["page-zero", "page-size-zero", "page-size-over-max"],
)
async def test_user_list_rejects_invalid_paging_params(admin_client, credentials, params):
    """page≥1、page_size 1~100 之外一律 422（矩阵行 1 的分页边界）。"""
    await _make_admin(admin_client, credentials)

    response = await admin_client.get("/api/admin/users", params=params)

    assert response.status_code == 422, response.text


async def test_user_list_stable_order_for_equal_created_at(admin_client, credentials):
    """created_at 相同的行按 id 同方向稳定排序：asc 升序、desc 降序（矩阵行 1）。"""
    await _make_admin(admin_client, credentials)
    tie_time = datetime(2020, 1, 1, tzinfo=UTC)  # 远早于库内其余账号，4 行同刻
    tie_ids = []
    for _ in range(4):
        user_id, _ = await _insert_user(created_at=tie_time)
        tie_ids.append(user_id)
    tie_ids.sort()

    ascending = (
        await admin_client.get(
            "/api/admin/users",
            params={"sort": "created_at", "order": "asc", "page_size": 100},
        )
    ).json()
    assert [item["id"] for item in ascending["items"]][:4] == tie_ids

    descending = (
        await admin_client.get(
            "/api/admin/users",
            params={"sort": "created_at", "order": "desc", "page_size": 100},
        )
    ).json()
    assert [item["id"] for item in descending["items"]][-4:] == list(reversed(tie_ids))


@pytest.mark.parametrize(
    "params",
    [
        {"sort": "role"},
        {"sort": "created_at", "order": "sideways"},
        {"created_from": "2026-01-01T00:00:00"},
        {
            "created_from": "2026-06-01T00:00:00+00:00",
            "created_to": "2026-01-01T00:00:00+00:00",
        },
    ],
    ids=["sort-out-of-whitelist", "order-invalid", "created-from-naive", "time-range-reversed"],
)
async def test_user_list_rejects_out_of_whitelist_sort_and_bad_time_filters(
    admin_client, credentials, params
):
    """白名单外 sort、非法 order、无时区时间、反向时间区间一律 422（矩阵行 1）。"""
    await _make_admin(admin_client, credentials)

    response = await admin_client.get("/api/admin/users", params=params)

    assert response.status_code == 422, response.text


# ---- 矩阵行 2（ADM-FR-005 / WI-AC-002）：筛选与统计 ----


async def test_user_list_filters_by_q_username_substring_and_full_uuid(
    admin_client, credentials
):
    """q 命中用户名包含或完整 UUID（矩阵行 2）。"""
    await _make_admin(admin_client, credentials)
    marker_id, marker_name = await _insert_user(username=f"test_marker_{uuid.uuid4().hex[:8]}")
    await _insert_user()  # 用户名不含 marker 的对照账号

    by_name = await admin_client.get(
        "/api/admin/users",
        params={"q": "marker", "sort": "created_at", "order": "desc", "page_size": 100},
    )
    assert by_name.status_code == 200, by_name.text
    expected = await _ordered_ids("desc", "username LIKE :pat", pat="%marker%")
    assert [item["id"] for item in by_name.json()["items"]] == expected
    assert marker_name in [item["username"] for item in by_name.json()["items"]]

    by_uuid = await admin_client.get(
        "/api/admin/users",
        params={"q": marker_id, "sort": "created_at", "order": "desc", "page_size": 100},
    )
    assert by_uuid.status_code == 200, by_uuid.text
    assert [item["id"] for item in by_uuid.json()["items"]] == [marker_id]


async def test_user_list_filters_by_status_and_created_range(admin_client, credentials):
    """status 与 created_from/created_to 筛选按契约口径过滤（矩阵行 2）。"""
    await _make_admin(admin_client, credentials)
    old_time = datetime(2020, 1, 5, tzinfo=UTC)  # 远早于库内其余账号
    old_id, _ = await _insert_user(created_at=old_time)
    disabled_id, _ = await _insert_user(created_at=old_time, status="disabled")

    by_status = await admin_client.get(
        "/api/admin/users",
        params={"status": "disabled", "sort": "created_at", "order": "desc", "page_size": 100},
    )
    assert by_status.status_code == 200, by_status.text
    expected_disabled = await _ordered_ids("desc", "status = 'disabled'")
    assert [item["id"] for item in by_status.json()["items"]] == expected_disabled
    assert disabled_id in expected_disabled

    window = {
        "created_from": "2020-01-01T00:00:00+00:00",
        "created_to": "2020-06-01T00:00:00+00:00",
    }
    oracle_window = {
        "lo": datetime(2020, 1, 1, tzinfo=UTC),
        "hi": datetime(2020, 6, 1, tzinfo=UTC),
    }
    # 恰好落在上界的账号：PRD §8.2 上界不包含，必须被排除出窗口
    boundary_id, _ = await _insert_user(created_at=oracle_window["hi"])
    by_range = await admin_client.get(
        "/api/admin/users",
        params=window | {"sort": "created_at", "order": "desc", "page_size": 100},
    )
    assert by_range.status_code == 200, by_range.text
    expected_range = await _ordered_ids(
        "desc", "created_at >= :lo AND created_at < :hi", **oracle_window
    )
    assert [item["id"] for item in by_range.json()["items"]] == expected_range
    assert {old_id, disabled_id} <= set(expected_range)
    assert boundary_id not in {item["id"] for item in by_range.json()["items"]}

    combined = await admin_client.get(
        "/api/admin/users",
        params=window | {"status": "active", "sort": "created_at", "order": "desc",
                         "page_size": 100},
    )
    assert combined.status_code == 200, combined.text
    expected_combined = await _ordered_ids(
        "desc", "status = 'active' AND created_at >= :lo AND created_at < :hi", **oracle_window
    )
    assert [item["id"] for item in combined.json()["items"]] == expected_combined
    assert old_id in expected_combined and disabled_id not in expected_combined


async def test_user_stats_count_hidden_and_derived_assets_and_split_run_counts(
    admin_client, client, credentials, other_credentials
):
    """统计含 library_hidden 与内部派生全 kind、两类任务分开计数、列表详情一致、
    total 不被 JOIN 抬高、last_login_at 登录前如实为空（矩阵行 2 / WI-AC-002）。"""
    await _make_admin(admin_client, credentials)

    # 目标账号样本：1 个会话（含上传原图）、1 个点选 MASK（隐藏派生 kind）、
    # 1 个 library_hidden 素材、2 个生成 ToolRun、1 个 AgentRun
    await client.post("/api/auth/register", json=other_credentials)
    target_id = (await client.get("/api/auth/me")).json()["id"]
    session = await open_session(client)  # 内部上传 1 张 original
    await create_mask(client, session)  # 派生 MASK（创意库不可见 kind）
    await _insert_hidden_asset(target_id, size_bytes=128)
    for _ in range(2):
        response = await client.post(
            "/api/generations",
            json={"prompt": "浅木色桌面上的白色马克杯", "ratio": "1:1", "count": 1},
        )
        assert response.status_code == 202, response.text
    await _insert_agent_run(target_id, session["id"], session["revision"])

    # 造数前提自检：样本与预期一致，防止对工具链行为的误设掩盖被测断言
    assert await _scalar("SELECT count(*) FROM assets WHERE user_id = :u",
                         u=uuid.UUID(target_id)) == 3
    assert await _scalar("SELECT count(*) FROM edit_sessions WHERE user_id = :u",
                         u=uuid.UUID(target_id)) == 1
    assert await _scalar("SELECT count(*) FROM tool_runs WHERE user_id = :u",
                         u=uuid.UUID(target_id)) == 2
    assert await _scalar("SELECT count(*) FROM agent_runs WHERE user_id = :u",
                         u=uuid.UUID(target_id)) == 1

    response = await admin_client.get(f"/api/admin/users/{target_id}")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["id"] == target_id
    assert detail["username"] == other_credentials["username"]
    assert detail["role"] == "user"
    assert detail["status"] == "active"
    assert detail["last_login_at"] is None  # 注册自动建会话不计登录，如实为空
    assert detail["asset_count"] == 3  # 含 MASK 派生与 library_hidden，全 kind
    assert detail["asset_record_bytes"] == await _scalar(
        "SELECT COALESCE(SUM(size_bytes), 0) FROM assets WHERE user_id = :u",
        u=uuid.UUID(target_id),
    )
    assert detail["session_count"] == 1
    assert detail["tool_run_count"] == 2  # 与 agent_run 分开计数，不得合并
    assert detail["agent_run_count"] == 1
    for leaked in ("password_hash", "token_version"):
        assert leaked not in detail

    listed = (
        await admin_client.get(
            "/api/admin/users",
            params={"q": other_credentials["username"], "sort": "created_at",
                    "order": "desc"},
        )
    ).json()
    assert listed["total"] == 1
    [item] = listed["items"]
    assert item == detail  # 列表与详情统计一致
    # 全表 total 不因统计 JOIN 抬高
    full = (await admin_client.get("/api/admin/users", params={"page_size": 100})).json()
    assert full["total"] == await _scalar("SELECT count(*) FROM users")


# ---- 矩阵行 3（ADM-FR-006 / WI-AC-003）：创建账号 ----


async def test_create_user_returns_201_without_touching_admin_session(
    admin_client, credentials
):
    """创建 201：不设置任何 Cookie、管理员会话原样可用、目标处于未登录态、新凭据可登录。"""
    await _make_admin(admin_client, credentials)
    admin_cookie = admin_client.cookies.get(SESSION_COOKIE)
    target_name = f"test_created_{uuid.uuid4().hex[:8]}"

    response = await admin_client.post(
        "/api/admin/users", json={"username": target_name, "password": "secret123"}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["username"] == target_name
    assert body["role"] == "user"
    for leaked in ("password_hash", "token_version"):
        assert leaked not in body
    assert "secret123" not in response.text  # 不回显凭据
    assert SESSION_COOKIE not in response.cookies  # 不调用 _start_session
    assert admin_client.cookies.get(SESSION_COOKIE) == admin_cookie  # 管理员 Cookie 不变
    # 管理员会话仍然可用
    assert (await admin_client.get("/api/admin/users")).status_code == 200
    # 目标处于未登录态，但新凭据可正常登录
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as fresh:
        assert (await fresh.get("/api/auth/me")).status_code == 401
        assert (
            await fresh.post(
                "/api/auth/login", json={"username": target_name, "password": "secret123"}
            )
        ).status_code == 200


async def test_create_user_rejects_duplicate_username_with_zero_write(
    admin_client, credentials
):
    """重名 409 username_taken，零写入且不回显密码（矩阵行 3）。"""
    await _make_admin(admin_client, credentials)
    target_name = f"test_created_{uuid.uuid4().hex[:8]}"
    first = await admin_client.post(
        "/api/admin/users", json={"username": target_name, "password": "secret123"}
    )
    assert first.status_code == 201, first.text
    before = await _scalar("SELECT count(*) FROM users")

    second = await admin_client.post(
        "/api/admin/users", json={"username": target_name, "password": "secret123"}
    )

    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "username_taken"
    assert await _scalar("SELECT count(*) FROM users") == before  # 零写入
    assert "secret123" not in second.text  # 错误不回显密码


@pytest.mark.parametrize(
    "payload",
    [
        {"username": "ab", "password": "zzzzz"},
        {"username": "bad name!", "password": "zzzzz"},
        {"username": "test_bad_pw", "password": "zzz"},
        {"username": "test_bad_pw", "password": "z" * 65},
    ],
    ids=["short-username", "bad-charset", "short-password", "long-password"],
)
async def test_create_user_rejects_invalid_credentials_with_zero_write(
    admin_client, credentials, payload
):
    """非法凭据 422，users 零写入，错误响应不回显密码（矩阵行 3）。"""
    await _make_admin(admin_client, credentials)
    before = await _scalar("SELECT count(*) FROM users")

    response = await admin_client.post("/api/admin/users", json=payload)

    assert response.status_code == 422, response.text
    assert await _scalar("SELECT count(*) FROM users") == before  # 零写入
    assert payload["password"] not in response.text  # 错误不回显密码


# ---- 矩阵行 4（ADM-FR-007 / WI-AC-004）：改名 ----


async def test_rename_user_preserves_identity_and_ownership(
    admin_client, client, credentials, other_credentials
):
    """改名 200：用户 id/归属不变、旧会话仍有效（token_version 不递增）、素材仍可访问。"""
    await _make_admin(admin_client, credentials)
    await client.post("/api/auth/register", json=other_credentials)
    target_id = (await client.get("/api/auth/me")).json()["id"]
    asset_id = await upload_asset(client)
    old_version = await _scalar(
        "SELECT token_version FROM users WHERE id = :u", u=uuid.UUID(target_id)
    )
    new_name = f"test_renamed_{uuid.uuid4().hex[:8]}"

    response = await admin_client.patch(
        f"/api/admin/users/{target_id}", json={"username": new_name}
    )

    assert response.status_code == 200, response.text
    assert response.json()["username"] == new_name
    # 身份锚点不变：旧会话（JWT 以 UUID 为锚）仍然有效
    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["id"] == target_id
    assert me.json()["username"] == new_name
    # 改名不递增 token_version
    assert (
        await _scalar("SELECT token_version FROM users WHERE id = :u", u=uuid.UUID(target_id))
    ) == old_version
    # 归属不变：改名前上传的素材仍归该用户
    assert (await client.get(f"/api/assets/{asset_id}")).status_code == 200


async def test_rename_user_conflict_is_409_with_zero_write(
    admin_client, client, credentials, other_credentials
):
    """改名为已有用户名 → 409 username_taken，双方用户名零变化（矩阵行 4）。"""
    admin = await _make_admin(admin_client, credentials)
    await client.post("/api/auth/register", json=other_credentials)
    target_id = (await client.get("/api/auth/me")).json()["id"]

    response = await admin_client.patch(
        f"/api/admin/users/{target_id}", json={"username": admin["username"]}
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "username_taken"
    assert (await _fetch_user_row(target_id))["username"] == other_credentials["username"]
    assert (await _fetch_user_row(admin["id"]))["username"] == admin["username"]


async def test_admin_can_rename_self(admin_client, credentials):
    """所有者可改名自身，改名后旧会话仍有效且仍是管理员（矩阵行 4）。"""
    admin = await _make_admin(admin_client, credentials)
    new_name = f"test_self_renamed_{uuid.uuid4().hex[:8]}"

    response = await admin_client.patch(
        f"/api/admin/users/{admin['id']}", json={"username": new_name}
    )

    assert response.status_code == 200, response.text
    me = await admin_client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == new_name
    assert (await admin_client.get("/api/admin/users")).status_code == 200  # 仍是管理员


# ---- 矩阵行 5（ADM-FR-008 / WI-AC-005）：重置密码 ----


async def test_reset_password_revokes_target_sessions_and_allows_new_login(
    admin_client, client, credentials, other_credentials
):
    """改密 204：目标旧会话 401、旧密码 401、新密码可登录；响应与审计无密码痕迹。"""
    admin = await _make_admin(admin_client, credentials)
    await client.post("/api/auth/register", json=other_credentials)
    target_id = (await client.get("/api/auth/me")).json()["id"]
    assert (await client.get("/api/auth/me")).status_code == 200

    response = await admin_client.post(
        f"/api/admin/users/{target_id}/reset-password", json={"password": "brand-new-456"}
    )

    assert response.status_code == 204, response.text
    assert SESSION_COOKIE not in response.cookies  # 不设置任何 Cookie
    assert "brand-new-456" not in response.text  # 不回显凭据
    # 目标旧会话立即失效
    assert (await client.get("/api/auth/me")).status_code == 401
    # 旧密码被拒、新密码可登录
    assert (await client.post("/api/auth/login", json=other_credentials)).status_code == 401
    assert (
        await client.post(
            "/api/auth/login",
            json={"username": other_credentials["username"], "password": "brand-new-456"},
        )
    ).status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 200
    # 审计行不含任何密码痕迹（矩阵行 5 后半）
    rows = await _audit_rows(admin["id"], target_id)
    assert any(row["action"] == "user_reset_password" for row in rows)
    assert all(
        "brand-new-456" not in row["summary"] and "secret123" not in row["summary"]
        for row in rows
    )


async def test_admin_self_reset_password_forces_relogin(admin_client, credentials):
    """管理员自改密：旧会话 401，需用新密码重新登录，登录后仍是管理员（矩阵行 5）。"""
    admin = await _make_admin(admin_client, credentials)

    response = await admin_client.post(
        f"/api/admin/users/{admin['id']}/reset-password", json={"password": "fresh-admin-789"}
    )

    assert response.status_code == 204, response.text
    # 管理员旧会话立即失效
    assert (await admin_client.get("/api/admin/users")).status_code == 401
    # 旧密码被拒；新密码重新登录后恢复管理员权限
    assert (
        await admin_client.post(
            "/api/auth/login",
            json={"username": admin["username"], "password": admin["password"]},
        )
    ).status_code == 401
    assert (
        await admin_client.post(
            "/api/auth/login",
            json={"username": admin["username"], "password": "fresh-admin-789"},
        )
    ).status_code == 200
    assert (await admin_client.get("/api/admin/users")).status_code == 200


# ---- 矩阵行 6（ADM-FR-009 / WI-AC-006）：禁用 / 启用 ----


async def test_disable_rejects_user_but_inflight_run_finishes(
    admin_client, client, credentials, other_credentials
):
    """禁用即拒（API 401）；在途 run 由 worker 照常收尾，不受用户状态影响（AC-018）。

    造 run 方式与 tests/test_generation.py 的 start_run 一致：mock provider 下
    POST /api/generations 后无 worker 消费，run 停在 queued，由本测试扮演 worker。
    """
    await _make_admin(admin_client, credentials)
    await client.post("/api/auth/register", json=other_credentials)
    target_id = (await client.get("/api/auth/me")).json()["id"]
    generation = await client.post(
        "/api/generations",
        json={"prompt": "浅木色桌面上的白色马克杯", "ratio": "1:1", "count": 1},
    )
    assert generation.status_code == 202, generation.text
    run_id = uuid.UUID(generation.json()["id"])

    response = await admin_client.patch(
        f"/api/admin/users/{target_id}", json={"status": "disabled"}
    )
    assert response.status_code == 200, response.text

    # 禁用即拒
    assert (await client.get("/api/auth/me")).status_code == 401
    assert (await client.post("/api/auth/login", json=other_credentials)).status_code == 401

    # worker 不查用户状态：在途 run 照常收尾
    await run_tool({}, run_id)
    async with SessionFactory() as session:
        run = await runs.load(session, run_id)
    assert run.status is RunStatus.SUCCEEDED
    assert run.progress == 100


async def test_pending_plan_stays_queued_across_disable_and_enable(
    admin_client, client, credentials, other_credentials, fake_planner
):
    """待确认计划在禁用与启用后都不被自动推进（矩阵行 6）。

    fake_planner 造多步计划的方式与 tests/test_agent.py 的待确认用例一致：
    多步计划停在 queued，各步骤 run_id 为空；推进只应由确认动作触发。
    """
    fake_planner(
        tool_calls(
            ("flip_layer", {"direction": "horizontal"}),
            ("rotate_layer", {"angle": 15}),
        )
    )
    await _make_admin(admin_client, credentials)
    await client.post("/api/auth/register", json=other_credentials)
    target_id = (await client.get("/api/auth/me")).json()["id"]
    session_id = (await open_session(client))["id"]
    turn = await send(client, session_id, "水平翻转再旋转 15 度")
    assert turn["status"] == "queued"  # 多步计划停在待确认

    assert (
        await admin_client.patch(
            f"/api/admin/users/{target_id}", json={"status": "disabled"}
        )
    ).status_code == 200
    assert (
        await admin_client.patch(
            f"/api/admin/users/{target_id}", json={"status": "active"}
        )
    ).status_code == 200

    # 禁用与启用全程无人推进：轮次仍 queued、任何步骤都未投递 run、无新 ToolRun
    status, plan = await _turn_state(turn["id"])
    assert status == "queued"
    assert all(not step.get("run_id") for step in plan)
    assert await _scalar(
        "SELECT count(*) FROM tool_runs WHERE user_id = :u", u=uuid.UUID(target_id)
    ) == 0


async def test_enable_does_not_revive_old_tokens(
    admin_client, client, credentials, other_credentials
):
    """启用不复活旧令牌：旧 Cookie 仍 401，重新登录后可用（矩阵行 6）。"""
    await _make_admin(admin_client, credentials)
    await client.post("/api/auth/register", json=other_credentials)
    target_id = (await client.get("/api/auth/me")).json()["id"]

    assert (
        await admin_client.patch(
            f"/api/admin/users/{target_id}", json={"status": "disabled"}
        )
    ).status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 401
    assert (
        await admin_client.patch(
            f"/api/admin/users/{target_id}", json={"status": "active"}
        )
    ).status_code == 200

    assert (await client.get("/api/auth/me")).status_code == 401  # 旧令牌不复活
    assert (await client.post("/api/auth/login", json=other_credentials)).status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 200


async def test_patch_status_same_value_is_idempotent(
    admin_client, client, credentials, other_credentials
):
    """status 相同值幂等 200、允许 username 与 status 并列提交且 token_version 不变
    （矩阵行 6 延伸，WI 实现要求 4）。"""
    await _make_admin(admin_client, credentials)
    await client.post("/api/auth/register", json=other_credentials)
    target_id = (await client.get("/api/auth/me")).json()["id"]
    before = await _fetch_user_row(target_id)

    idempotent = await admin_client.patch(
        f"/api/admin/users/{target_id}", json={"status": "active"}
    )
    assert idempotent.status_code == 200, idempotent.text

    new_name = f"test_renamed_{uuid.uuid4().hex[:8]}"
    combined = await admin_client.patch(
        f"/api/admin/users/{target_id}", json={"username": new_name, "status": "active"}
    )
    assert combined.status_code == 200, combined.text

    after = await _fetch_user_row(target_id)
    assert _value(after["status"]) == "active"
    assert after["username"] == new_name
    assert after["token_version"] == before["token_version"]


# ---- 矩阵行 7（ADM-FR-010 / WI-AC-007）：所有者保护 ----


async def test_owner_cannot_disable_self(admin_client, credentials):
    """对自身 PATCH status=disabled → 409 owner_protected，状态与会话不受影响（矩阵行 7）。"""
    admin = await _make_admin(admin_client, credentials)

    response = await admin_client.patch(
        f"/api/admin/users/{admin['id']}", json={"status": "disabled"}
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "owner_protected"
    assert _value((await _fetch_user_row(admin["id"]))["status"]) == "active"  # 零越权写入
    assert (await admin_client.get("/api/admin/users")).status_code == 200  # 会话不受影响


@pytest.mark.parametrize(
    "payload",
    [
        {"role": "user"},
        {"role": "admin"},
        {"token_version": 99},
        {"last_login_at": "2020-01-01T00:00:00+00:00"},
        {"status": "active", "role": "admin"},
    ],
    ids=["role-down", "role-up", "token-version", "last-login", "mixed-status-role"],
)
async def test_protected_fields_are_never_writable_for_any_target(
    admin_client, client, credentials, other_credentials, payload
):
    """role/token_version/last_login_at 永不可写（schema extra=forbid → 422），
    对自身与普通用户一视同仁，全部尝试零写入（矩阵行 7）。"""
    admin = await _make_admin(admin_client, credentials)
    await client.post("/api/auth/register", json=other_credentials)
    target_id = (await client.get("/api/auth/me")).json()["id"]

    for victim_id in (admin["id"], target_id):
        response = await admin_client.patch(f"/api/admin/users/{victim_id}", json=payload)
        assert response.status_code == 422, f"{victim_id}: {response.text}"

    # 零越权写入：双方 role/status/token_version 保持原值
    admin_row = await _fetch_user_row(admin["id"])
    assert _value(admin_row["role"]) == "admin"
    assert _value(admin_row["status"]) == "active"
    target_row = await _fetch_user_row(target_id)
    assert _value(target_row["role"]) == "user"
    assert _value(target_row["status"]) == "active"
    assert target_row["token_version"] == 1


async def test_admin_user_detail_404_for_missing_id(admin_client, credentials):
    """不存在的用户 ID → 统一 404 结构（矩阵行 7 附属：404 不区分不存在与非存在 ID）。"""
    await _make_admin(admin_client, credentials)

    response = await admin_client.get(f"/api/admin/users/{uuid.uuid4()}")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert isinstance(detail, dict), detail  # Red：兜底路由返回字符串 detail，在此失败
    assert detail["code"] == "not_found"


# ---- 矩阵行 8（ADM-FR-002/003 / WI-AC-008）：端点级 401/403 ----


def _probes() -> list[tuple[str, str, dict | None]]:
    """全部新管理端点的探针：静态路径与 /{id} 路径各分支。"""
    missing_id = str(uuid.uuid4())
    return [
        ("GET", "/api/admin/users", None),
        (
            "POST",
            "/api/admin/users",
            {"username": f"test_probe_{uuid.uuid4().hex[:8]}", "password": "secret123"},
        ),
        ("GET", f"/api/admin/users/{missing_id}", None),
        ("PATCH", f"/api/admin/users/{missing_id}",
         {"username": f"test_probe_{uuid.uuid4().hex[:8]}"}),
        ("POST", f"/api/admin/users/{missing_id}/reset-password", {"password": "secret123"}),
    ]


_PROBE_IDS = ["list", "create", "detail", "patch", "reset-password"]


@pytest.mark.parametrize(("method", "path", "body"), _probes(), ids=_PROBE_IDS)
async def test_admin_endpoints_require_authentication(method, path, body):
    """全部新管理端点未登录一律 401（矩阵行 8，闭合阶段 1 遗留 AC-003）。"""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as anonymous:
        response = await anonymous.request(method, path, json=body)
    assert response.status_code == 401, response.text


@pytest.mark.parametrize(("method", "path", "body"), _probes(), ids=_PROBE_IDS)
async def test_admin_endpoints_reject_non_admin_users(
    client, credentials, method, path, body
):
    """全部新管理端点对普通用户一律 403（矩阵行 8，闭合阶段 1 遗留 AC-003）。"""
    await client.post("/api/auth/register", json=credentials)

    response = await client.request(method, path, json=body)

    assert response.status_code == 403, response.text


# ---- 矩阵行 9（ADM-FR-023 / WI-AC-009）：审计一致性 ----


async def test_write_actions_audited_with_actor_target_action_result(
    admin_client, credentials
):
    """每类写动作成功留痕：actor/target_type/target_id/action/result 全部正确，
    summary 无密码哈希痕迹（矩阵行 9）。"""
    admin = await _make_admin(admin_client, credentials)
    target_name = f"test_audit_{uuid.uuid4().hex[:8]}"
    created = await admin_client.post(
        "/api/admin/users", json={"username": target_name, "password": "audit-pass-1"}
    )
    assert created.status_code == 201, created.text
    target_id = created.json()["id"]

    assert (
        await admin_client.patch(
            f"/api/admin/users/{target_id}",
            json={"username": f"test_audit_renamed_{uuid.uuid4().hex[:6]}"},
        )
    ).status_code == 200
    assert (
        await admin_client.post(
            f"/api/admin/users/{target_id}/reset-password", json={"password": "audit-pass-2"}
        )
    ).status_code == 204
    assert (
        await admin_client.patch(
            f"/api/admin/users/{target_id}", json={"status": "disabled"}
        )
    ).status_code == 200
    assert (
        await admin_client.patch(
            f"/api/admin/users/{target_id}", json={"status": "active"}
        )
    ).status_code == 200

    rows = await _audit_rows(admin["id"], target_id)
    assert [(row["action"], row["result"]) for row in rows] == [
        ("user_create", "success"),
        ("user_rename", "success"),
        ("user_reset_password", "success"),
        ("user_disable", "success"),
        ("user_enable", "success"),
    ]
    assert all(row["actor_id"] == admin["id"] for row in rows)
    assert all(row["target_type"] == "user" for row in rows)
    assert all(row["target_id"] == target_id for row in rows)
    # summary 白名单键值，绝无密码/哈希/令牌痕迹
    for row in rows:
        assert "audit-pass-1" not in row["summary"]
        assert "audit-pass-2" not in row["summary"]
        assert "password" not in row["summary"].lower()
        assert "hash" not in row["summary"].lower()
        assert "token" not in row["summary"].lower()


async def test_rejected_writes_record_rejected_audit_rows(
    admin_client, client, credentials, other_credentials
):
    """预期拒绝（409）在业务回滚后另起事务补记 result=rejected（矩阵行 9）。"""
    admin = await _make_admin(admin_client, credentials)
    await client.post("/api/auth/register", json=other_credentials)
    target_id = (await client.get("/api/auth/me")).json()["id"]

    conflict = await admin_client.patch(
        f"/api/admin/users/{target_id}", json={"username": admin["username"]}
    )
    assert conflict.status_code == 409, conflict.text
    duplicate = await admin_client.post(
        "/api/admin/users",
        json={"username": other_credentials["username"], "password": "secret123"},
    )
    assert duplicate.status_code == 409, duplicate.text

    # 改名拒绝：target 明确存在，target_id 即该用户
    rows = await _audit_rows(admin["id"], target_id)
    assert [(row["action"], row["result"]) for row in rows] == [("user_rename", "rejected")]
    # 拒绝不产生业务写入：目标用户名保持不变
    assert (await _fetch_user_row(target_id))["username"] == other_credentials["username"]
    # 重复创建的拒绝同样补记（目标不存在，target_id 由实现定义，只核对 action/result）
    created_rows = await _audit_rows(admin["id"])
    assert ("user_create", "rejected") in [
        (row["action"], row["result"]) for row in created_rows
    ]


async def test_list_reads_do_not_write_audit_rows(
    admin_client, client, credentials, other_credentials
):
    """普通列表/详情读取不产生审计行（PRD FR-024）（矩阵行 9）。"""
    admin = await _make_admin(admin_client, credentials)
    await client.post("/api/auth/register", json=other_credentials)
    target_id = (await client.get("/api/auth/me")).json()["id"]

    # Red：端点缺失，此处的 404 即本用例的具体失败
    assert (await admin_client.get("/api/admin/users")).status_code == 200
    before = await _audit_count(admin["id"])
    assert (await admin_client.get("/api/admin/users")).status_code == 200
    assert (await admin_client.get(f"/api/admin/users/{target_id}")).status_code == 200
    assert await _audit_count(admin["id"]) == before
