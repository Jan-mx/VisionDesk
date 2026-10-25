"""WI-20261025-01 权限与会话基础专项测试（Red 阶段）。

对应测试矩阵 10 行（docs/work-items/WI-20261025-01-admin-auth-foundation.md），
每条用例 docstring 标注所属矩阵行与验收场景。Red 约定：

- 尚不存在的新模块（``app.init_admin``、``app.deps.require_admin``）一律在测试
  函数体内惰性导入，保证实现前文件可正常收集，模块缺失即该用例的 Red；
- 新列（role/status/token_version/last_login_at）尚不存在 ORM 字段，DB 状态
  操纵与读取全部走 SQLAlchemy ``text()`` 直改直查。
"""

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import SessionFactory
from app.security import SESSION_COOKIE


def _value(raw):
    """text() 直查返回原始字符串；实现后经枚举列映射读出的是枚举成员，统一还原为值。"""
    return getattr(raw, "value", raw)


async def _scalar(sql: str, **params):
    async with SessionFactory() as session:
        return (await session.execute(text(sql), params)).scalar_one()


async def _fetch_user(username: str) -> dict:
    async with SessionFactory() as session:
        row = await session.execute(
            text(
                "SELECT role, status, token_version, last_login_at"
                " FROM users WHERE username = :u"
            ),
            {"u": username},
        )
        return row.mappings().one()


async def _admin_usernames() -> list[str]:
    async with SessionFactory() as session:
        rows = await session.execute(
            text("SELECT username FROM users WHERE role = 'admin'")
        )
        return rows.scalars().all()


async def _require_no_external_admin() -> None:
    """唯一管理员索引覆盖整张 users 表（矩阵行 2），而测试只增删 test_ 前缀账号。

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


# ---- 矩阵行 1（ADM-FR-001 / WI-AC-001）：init_admin ----


async def test_init_admin_promotes_named_user_to_only_admin(
    client: httpx.AsyncClient, credentials, other_credentials
):
    """显式指定账号提升为管理员，且提升后全库仅此一位管理员。"""
    from app.init_admin import promote_admin  # 惰性导入：实现前无此模块，缺失即本用例 Red

    await _require_no_external_admin()
    assert (await client.post("/api/auth/register", json=credentials)).status_code == 201
    assert (
        await client.post("/api/auth/register", json=other_credentials)
    ).status_code == 201

    async with SessionFactory() as session:
        await promote_admin(session, credentials["username"])
        await session.commit()

    assert await _admin_usernames() == [credentials["username"]]
    row = await _fetch_user(other_credentials["username"])
    assert _value(row["role"]) == "user"


async def test_init_admin_is_idempotent(client: httpx.AsyncClient, credentials):
    """目标已是 admin 时重复执行幂等成功，不报错也不产生变化。"""
    from app.init_admin import promote_admin

    await _require_no_external_admin()
    await client.post("/api/auth/register", json=credentials)

    async with SessionFactory() as session:
        await promote_admin(session, credentials["username"])
        await session.commit()

    async with SessionFactory() as session:
        await promote_admin(session, credentials["username"])  # 第二次不得抛错
        await session.commit()

    assert await _admin_usernames() == [credentials["username"]]


async def test_init_admin_rejects_second_admin(
    client: httpx.AsyncClient, credentials, other_credentials
):
    """已有其他管理员时拒绝提升，管理员集合保持不变。"""
    from app.init_admin import promote_admin

    await _require_no_external_admin()
    await client.post("/api/auth/register", json=credentials)
    await client.post("/api/auth/register", json=other_credentials)

    async with SessionFactory() as session:
        await promote_admin(session, credentials["username"])
        await session.commit()

    async with SessionFactory() as session:
        with pytest.raises(Exception):
            await promote_admin(session, other_credentials["username"])
        await session.rollback()  # 丢弃可能的部分变更，由下方终态断言核对

    assert await _admin_usernames() == [credentials["username"]]


async def test_init_admin_rejects_unknown_user(client: httpx.AsyncClient, credentials):
    """用户不存在时明确报错，管理员集合不变。"""
    from app.init_admin import promote_admin

    await _require_no_external_admin()
    await client.post("/api/auth/register", json=credentials)
    admins_before = await _admin_usernames()

    async with SessionFactory() as session:
        with pytest.raises(Exception):
            await promote_admin(session, f"test_ghost_{uuid.uuid4().hex[:10]}")
        await session.rollback()

    assert await _admin_usernames() == admins_before


# ---- 矩阵行 2（ADM-FR-001 / ADM-FR-027 迁移部分）：默认值与唯一索引 ----
# （迁移三连 upgrade→downgrade→upgrade 由主会话在隔离库执行，见 WI 验证命令表。）


async def test_user_defaults_after_migration(client: httpx.AsyncClient, credentials):
    """新账号与模拟的迁移前旧账号（省略全部新列直插）默认 user/active/ver=1、登录时间为空。

    旧账号经 text() 直插绕过 ORM，默认值必须来自迁移 server_default，
    不能只靠 ORM Python 侧默认。
    """
    assert (await client.post("/api/auth/register", json=credentials)).status_code == 201

    legacy_username = f"test_{uuid.uuid4().hex[:10]}"
    async with SessionFactory() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, username, password_hash)"
                " VALUES (:id, :username, :password_hash)"
            ),
            {"id": uuid.uuid4(), "username": legacy_username, "password_hash": "legacy-hash"},
        )
        await session.commit()

    for username in (credentials["username"], legacy_username):
        row = await _fetch_user(username)
        assert _value(row["role"]) == "user", username
        assert _value(row["status"]) == "active", username
        assert row["token_version"] == 1, username
        assert row["last_login_at"] is None, username


async def test_partial_unique_index_blocks_second_admin(
    client: httpx.AsyncClient, credentials, other_credentials
):
    """直改第二个 admin 违反部分唯一索引 uq_users_single_admin。"""
    await client.post("/api/auth/register", json=credentials)
    await client.post("/api/auth/register", json=other_credentials)
    await _require_no_external_admin()

    async with SessionFactory() as session:
        await session.execute(
            text("UPDATE users SET role = 'admin' WHERE username = :u"),
            {"u": credentials["username"]},
        )
        await session.commit()

    with pytest.raises(IntegrityError):
        async with SessionFactory() as session:
            await session.execute(
                text("UPDATE users SET role = 'admin' WHERE username = :u"),
                {"u": other_credentials["username"]},
            )
            await session.commit()


# ---- 矩阵行 3（ADM-FR-002）：禁用即拒 ----


async def test_disabled_user_session_rejected_and_login_generic_failure(
    client: httpx.AsyncClient, credentials
):
    """DB 置 disabled 后旧 cookie 访问 /me 401（文案复用不泄漏细节）；登录走通用认证失败。"""
    await client.post("/api/auth/register", json=credentials)

    async with SessionFactory() as session:
        await session.execute(
            text("UPDATE users SET status = 'disabled' WHERE username = :u"),
            {"u": credentials["username"]},
        )
        await session.commit()

    response = await client.get("/api/auth/me")
    assert response.status_code == 401
    assert response.json()["detail"] == "未登录或会话已过期"  # 不泄漏「已禁用」等细节

    response = await client.post("/api/auth/login", json=credentials)
    assert response.status_code == 401
    assert response.json()["detail"] == "用户名或密码错误"  # 通用失败，不区分禁用


# ---- 矩阵行 4（ADM-FR-002）：token_version 撤销 ----


async def test_token_version_bump_invalidates_old_token(
    client: httpx.AsyncClient, credentials
):
    """token_version 递增后旧令牌 401，重新登录获得的新会话可用。"""
    await client.post("/api/auth/register", json=credentials)
    assert (await client.get("/api/auth/me")).status_code == 200

    async with SessionFactory() as session:
        await session.execute(
            text("UPDATE users SET token_version = token_version + 1 WHERE username = :u"),
            {"u": credentials["username"]},
        )
        await session.commit()

    assert (await client.get("/api/auth/me")).status_code == 401

    assert (await client.post("/api/auth/login", json=credentials)).status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 200


async def test_reenabling_user_does_not_revive_old_token(
    client: httpx.AsyncClient, credentials
):
    """禁用并递增版本后恢复 active，旧令牌仍因版本不匹配保持 401（WI-AC-004 后半）。"""
    await client.post("/api/auth/register", json=credentials)

    async with SessionFactory() as session:
        await session.execute(
            text(
                "UPDATE users SET status = 'disabled', token_version = token_version + 1"
                " WHERE username = :u"
            ),
            {"u": credentials["username"]},
        )
        await session.commit()

    assert (await client.get("/api/auth/me")).status_code == 401

    async with SessionFactory() as session:
        await session.execute(
            text("UPDATE users SET status = 'active' WHERE username = :u"),
            {"u": credentials["username"]},
        )
        await session.commit()

    assert (await client.get("/api/auth/me")).status_code == 401


# ---- 矩阵行 5（ADM-FR-002 / WI-AC-004）：旧格式 JWT ----


async def test_legacy_jwt_without_ver_is_rejected(client: httpx.AsyncClient, credentials):
    """迁移前签发的无 ver 旧格式 JWT 必须 401，要求重新登录。"""
    response = await client.post("/api/auth/register", json=credentials)
    user_id = response.json()["id"]
    await client.post("/api/auth/logout")  # 清掉 jar 里的现行会话，避免与显式 Cookie 头混叠

    legacy_token = jwt.encode(
        {"sub": user_id, "exp": datetime.now(UTC) + timedelta(hours=1)},
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    response = await client.get(
        "/api/auth/me", headers={"Cookie": f"{SESSION_COOKIE}={legacy_token}"}
    )

    assert response.status_code == 401


# ---- 矩阵行 6（ADM-FR-002 / WI-AC-005）：SSE 中途失效 ----


async def test_sse_stream_terminates_within_20s_after_revocation(
    client: httpx.AsyncClient, credentials
):
    """流中途递增 token_version，SSE 必须在 20 秒内断流（15s tick + 余量）。

    造 run 方式与 tests/test_generation.py 的 start_run 一致：mock provider 下
    POST /api/generations 后无 worker 消费，run 停在 queued，流只发快照与心跳。

    httpx 的 ASGITransport 会缓冲整个响应体（流结束前读不到任何行），因此撤销
    必须由后台消费任务并行进行：先起流，1 秒后撤销，再限时等待流结束——保证
    撤销确实发生在流存活期间，而不是流结束后才补一刀。
    """
    await client.post("/api/auth/register", json=credentials)
    response = await client.post(
        "/api/generations",
        json={"prompt": "浅木色桌面上的白色马克杯", "ratio": "1:1", "count": 1},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]

    frames: list[str] = []

    async def consume() -> None:
        async with client.stream("GET", f"/events/runs/{run_id}") as stream:
            async for line in stream.aiter_lines():
                frames.append(line)

    task = asyncio.create_task(consume())
    try:
        await asyncio.sleep(1.0)  # 留出建流与快照时间，撤销发生在流中途
        async with SessionFactory() as session:
            await session.execute(
                text(
                    "UPDATE users SET token_version = token_version + 1"
                    " WHERE username = :u"
                ),
                {"u": credentials["username"]},
            )
            await session.commit()

        started = time.monotonic()
        await asyncio.wait_for(task, timeout=20.0)
        assert frames and frames[0].startswith("data: "), frames[:1]
    except asyncio.TimeoutError:
        pytest.fail(
            f"SSE 流在撤销后 20 秒内未断流（已等待 {time.monotonic() - started:.1f}s）"
        )
    finally:
        if not task.done():
            task.cancel()


# ---- 矩阵行 7（ADM-FR-002）：require_admin 三分支 ----


async def test_require_admin_three_branches(client: httpx.AsyncClient, credentials):
    """测试内建临时 FastAPI 路由挂依赖：无认证 401 / 普通用户 403(detail=forbidden) / 管理员放行。"""
    from app.deps import require_admin  # 惰性导入：实现前 deps 无此依赖，缺失即本用例 Red

    await _require_no_external_admin()

    probe = FastAPI()

    @probe.get("/admin-only", dependencies=[Depends(require_admin)])
    async def admin_only() -> dict:
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=ASGITransport(app=probe), base_url="http://probe"
    ) as probe_client:
        # 分支一：无认证
        assert (await probe_client.get("/admin-only")).status_code == 401

        # 分支二：普通用户
        assert (await client.post("/api/auth/register", json=credentials)).status_code == 201
        probe_client.headers["Cookie"] = (
            f"{SESSION_COOKIE}={client.cookies.get(SESSION_COOKIE)}"
        )
        response = await probe_client.get("/admin-only")
        assert response.status_code == 403
        assert response.json()["detail"] == "forbidden"

        # 分支三：管理员放行（DB 直提，模拟 init_admin 之后的状态）
        async with SessionFactory() as session:
            await session.execute(
                text("UPDATE users SET role = 'admin' WHERE username = :u"),
                {"u": credentials["username"]},
            )
            await session.commit()

        assert (await probe_client.get("/admin-only")).status_code == 200


# ---- 矩阵行 8（ADM-FR-001/002 / WI-AC-002）：schema 注入拒绝 ----


async def test_register_rejects_injected_admin_fields(client: httpx.AsyncClient, credentials):
    """注册注入 role/token_version/status/last_login_at → 422 且 users 计数不变。"""
    before = await _scalar("SELECT count(*) FROM users")

    response = await client.post(
        "/api/auth/register",
        json={
            **credentials,
            "role": "admin",
            "token_version": 99,
            "status": "disabled",
            "last_login_at": "2020-01-01T00:00:00+00:00",
        },
    )

    assert response.status_code == 422
    assert await _scalar("SELECT count(*) FROM users") == before  # 零写入


async def test_login_rejects_injected_admin_fields(client: httpx.AsyncClient, credentials):
    """登录注入 role/token_version → 422 且 users 计数不变。"""
    await client.post("/api/auth/register", json=credentials)
    before = await _scalar("SELECT count(*) FROM users")

    response = await client.post(
        "/api/auth/login", json={**credentials, "role": "admin", "token_version": 99}
    )

    assert response.status_code == 422
    assert await _scalar("SELECT count(*) FROM users") == before  # 零写入


# ---- 矩阵行 9（ADM-FR-002）：响应增补 role 与 last_login_at ----


async def test_auth_responses_expose_role(client: httpx.AsyncClient, credentials):
    """register/login/me 响应含 role=user，且不暴露 password_hash/token_version。"""
    response = await client.post("/api/auth/register", json=credentials)
    assert response.status_code == 201
    body = response.json()
    assert body["role"] == "user"
    assert "password_hash" not in body
    assert "token_version" not in body

    await client.post("/api/auth/logout")
    response = await client.post("/api/auth/login", json=credentials)
    assert response.status_code == 200
    assert response.json()["role"] == "user"

    response = await client.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json()["role"] == "user"


async def test_last_login_at_null_until_login_then_updated(
    client: httpx.AsyncClient, credentials
):
    """last_login_at 注册（首登前）为空，登录后更新；注册自动建会话不计为登录。"""
    await client.post("/api/auth/register", json=credentials)

    row = await _fetch_user(credentials["username"])
    assert row["last_login_at"] is None

    await client.post("/api/auth/logout")
    assert (await client.post("/api/auth/login", json=credentials)).status_code == 200

    row = await _fetch_user(credentials["username"])
    assert row["last_login_at"] is not None
