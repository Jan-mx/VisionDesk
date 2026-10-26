"""WI-20261025-04 管理端素材专项测试（Red 阶段）。

对应测试矩阵全部 6 行（docs/work-items/WI-20261025-04-admin-assets.md，
ADM-FR-017~020、ADM-FR-023 素材动作、ADM-FR-004 回归），每条用例 docstring
标注所属矩阵行与验收场景（WI-AC-001~007 = ADM-AC-035~041 逐条）。Red 与
造数约定：

- 阶段 4 新端点（``/api/admin/assets`` 全套）尚不存在：请求落入 ``main.py``
  的 ``/api`` 兜底路由得到 404 ``{"detail": "Not Found"}``，每条用例在第一个
  涉及新端点的断言上以具体断言失败呈现 Red；
- 新服务函数 ``services/assets.py::asset_references`` 尚不存在：唯一涉及的用例
  在测试体内惰性导入（Red 呈现 ImportError），本文件无模块级缺失导入，天然
  可收集、不存在收集错误；
- 管理员造数沿用阶段 2 ``test_admin_users.py`` 的先例（``_make_admin``）：
  注册 ``test_`` 账号后 SQL 直改 ``role='admin'``，不依赖尚不存在的管理端点；
- 素材/会话造数复用 ``tests/test_assets.py`` 手法（upload_asset / open_session /
  create_mask / seed_wall_asset / attach_to_wall / storage_key_of）；历史与图层
  引用构造沿用 ``test_deletion_safety.py`` 的 DB 直插先例（EditHistory 与
  edit_sessions.document）；
- 开发库存在非 ``test_`` 前缀的真实账号与素材（手工冒烟遗留）：凡涉及列表
  total、排序、筛选的断言，一律以 WI 固化契约的同口径 SQL 为预期基准（同过滤
  条件、同排序键、id 同方向稳定次序），不假设库内只有测试数据；
- 审计断言全部走 SQLAlchemy ``text()`` 直查 ``admin_audit_logs``（与阶段 2 一致）；
- 单删 deleted/hidden 两态的 HTTP 形状 WI 决策 4 未固化（204 或 200 均可能）：
  用例以「状态码 ∈ {200, 204} + DB 行/对象/墙关联语义」锚定两态，不断言具体
  body 键名；批删突发错误的「响应附部分结果说明」以已删 ID 出现在响应文本为
  锚（WI 决策 5 原文要求）。
"""

import json
import logging
import re
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import text

from app import storage
from app.db import SessionFactory
from app.main import app
from app.models import Asset, EditHistory
from app.services import runs
from tests.canvas import apply
from tests.test_assets import (
    attach_to_wall,
    create_mask,
    open_session,
    seed_wall_asset,
    storage_key_of,
    upload_asset,
)

# ---- 造数与断言助手（沿用阶段 2/3 与 test_deletion_safety 的先例）----


@pytest.fixture
def third_credentials() -> dict[str, str]:
    """第三个账号凭据：管理员（credentials）+ 两个目标用户时的第二个用户。"""
    return {"username": f"test_{uuid.uuid4().hex[:10]}", "password": "secret123"}


@pytest.fixture
async def second_client():
    """第二支普通用户客户端：与 ``client`` 互不串 Cookie，用于跨用户场景。"""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def admin_client():
    """独立于目标用户 ``client`` 的管理员客户端：承载管理员会话，互不串 Cookie
    （与 tests/test_admin_users.py 同款）。"""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _require_no_external_admin() -> None:
    """与 tests/test_admin_users.py 同一前置：测试只增删 test_ 前缀账号。"""
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


async def _register(client: httpx.AsyncClient, credentials: dict) -> str:
    """注册 test_ 账号（注册即自动登录），返回 user id。"""
    assert (await client.post("/api/auth/register", json=credentials)).status_code == 201
    me = await client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    return me.json()["id"]


async def _scalar(sql: str, **params):
    async with SessionFactory() as session:
        return (await session.execute(text(sql), params)).scalar_one()


async def _asset_ids(where: str = "TRUE", order: str = "desc", **params) -> list[str]:
    """列表断言的契约口径 oracle（WI 决策 1）：主排序 + id 同方向稳定次序。

    由 WI 文本转写而非读取实现；开发库的非测试数据经由相同过滤条件纳入预期。
    """
    assert order in ("asc", "desc")
    async with SessionFactory() as session:
        rows = await session.execute(
            text(f"SELECT id::text FROM assets WHERE {where}"
                 f" ORDER BY created_at {order}, id {order}"),
            params,
        )
        return rows.scalars().all()


async def _insert_asset(
    user_id: str,
    *,
    kind: str = "original",
    source: str = "upload",
    library_hidden: bool = False,
    size_bytes: int = 128,
    created_at: datetime | None = None,
    width: int = 64,
    height: int = 64,
) -> str:
    """DB 直插素材行（无对象存储残留）；返回 id。字段先例同 test_admin_users。"""
    asset_id = uuid.uuid4()
    params: dict = {
        "id": asset_id,
        "user_id": uuid.UUID(user_id),
        "key": f"test/admin-assets/{uuid.uuid4()}",
        "kind": kind,
        "source": source,
        "size": size_bytes,
        "hidden": library_hidden,
        "width": width,
        "height": height,
    }
    columns = (
        "id, user_id, kind, source, storage_key, image_format, width, height,"
        " size_bytes, has_alpha, library_hidden"
    )
    values = (
        ":id, :user_id, :kind, :source, :key, 'PNG', :width, :height,"
        " :size, false, :hidden"
    )
    if created_at is not None:
        columns += ", created_at"
        values += ", :created_at"
        params["created_at"] = created_at
    async with SessionFactory() as session:
        await session.execute(
            text(f"INSERT INTO assets ({columns}) VALUES ({values})"), params
        )
        await session.commit()
    return str(asset_id)


async def _add_document_layer(session_id: str, asset_id: str) -> None:
    """把素材追加为会话当前 document 的图层引用（先例：test_deletion_safety）。

    追加的图层满足 Layer 模型必填字段（id/kind/name/width/height），document
    仍可被 LayerDocument.model_validate 解析，不触发 reference_state_invalid。
    """
    async with SessionFactory() as session:
        row = (
            await session.execute(
                text("SELECT document FROM edit_sessions WHERE id = :s"),
                {"s": uuid.UUID(session_id)},
            )
        ).one()
        document = dict(row.document or {})
        layers = list(document.get("layers", []))
        layers.append(
            {
                "id": f"ext_{uuid.uuid4().hex[:6]}",
                "kind": "image",
                "name": "外部图层",
                "width": 64,
                "height": 64,
                "asset_id": asset_id,
            }
        )
        document["layers"] = layers
        await session.execute(
            text("UPDATE edit_sessions SET document = CAST(:doc AS jsonb) WHERE id = :s"),
            {"doc": json.dumps(document, ensure_ascii=False), "s": uuid.UUID(session_id)},
        )
        await session.commit()


async def _add_history_reference(
    user_id: str, session_id: str, seq: int, asset_id: str
) -> None:
    """直插一条保留历史，params.before 的 current_asset_id 引用指定素材
    （先例：test_deletion_safety.test_delete_blocked_lists_blocking_references_only）。

    result 不含该素材（_collect_state 对缺失键直接跳过），保证每条历史恰好
    产生一个引用该素材的历史文档，供 layer_in 计数断言使用。
    """
    async with SessionFactory() as session:
        session.add(
            EditHistory(
                user_id=uuid.UUID(user_id),
                session_id=uuid.UUID(session_id),
                seq=seq,
                action="admin-test-note",
                params={"before": {"current_asset_id": asset_id, "revision": 1}},
                result={"ok": True, "revision": seq},
            )
        )
        await session.commit()


async def _wall_count(asset_id: str) -> int:
    return await _scalar(
        "SELECT count(*) FROM session_assets WHERE asset_id = :a", a=uuid.UUID(asset_id)
    )


async def _asset_row(asset_id: str) -> dict | None:
    async with SessionFactory() as session:
        row = await session.execute(
            text("SELECT kind::text AS kind, library_hidden, title"
                 " FROM assets WHERE id = :a"),
            {"a": uuid.UUID(asset_id)},
        )
        mapped = row.mappings().first()
        return dict(mapped) if mapped else None


async def _audit_rows(actor_id: str, target_id: str | None = None) -> list[dict]:
    """text() 直查审计表（表自阶段 2 已存在，可直查）。"""
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
    return await _scalar(
        "SELECT count(*) FROM admin_audit_logs WHERE actor_id = :a", a=actor_id
    )


def _types_by_session(references: list[dict]) -> dict[str, set[str]]:
    """详情完整引用行（{session_id, title, types}）转为 {session_id: {types}}。"""
    return {row["session_id"]: set(row["types"]) for row in references}


def _probe_list() -> list[tuple[str, str, dict | None]]:
    """全部新管理素材端点的探针：静态路径与 /{id} 路径各分支。"""
    missing_id = str(uuid.uuid4())
    return [
        ("GET", "/api/admin/assets", None),
        ("GET", f"/api/admin/assets/{missing_id}", None),
        ("PATCH", f"/api/admin/assets/{missing_id}", {"title": "x"}),
        ("DELETE", f"/api/admin/assets/{missing_id}", None),
        ("POST", "/api/admin/assets/batch-delete", {"asset_ids": [missing_id]}),
    ]


_PROBE_IDS = ["list", "detail", "patch", "delete", "batch-delete"]


# ---- 矩阵行 1（ADM-FR-017 / WI-AC-001=ADM-AC-035）：筛选、分页与 total ----


async def test_admin_asset_list_filters_by_user_kind_source_hidden_and_range(
    admin_client, client, second_client, credentials, other_credentials,
    third_credentials,
):
    """跨用户 user_id/kind/source/library_hidden 组合筛选与 created_from/to
    （下含上不含）；total 与同口径 SQL 一致（JOIN users 不抬高 total）；列表项
    含 user_id/username 归属且不含 storage_key（矩阵行 1 / ADM-AC-035）。"""
    await _make_admin(admin_client, credentials)
    a_id = await _register(client, other_credentials)
    b_id = await _register(second_client, third_credentials)

    await upload_asset(client)
    mask_session = await open_session(client)  # 内部上传 1 张 original
    mask_id = await create_mask(client, mask_session)  # 内部派生 kind=mask
    generated_id = await _insert_asset(a_id, kind="generated", source="generate")
    await _insert_asset(a_id, library_hidden=True)
    b_upload = await upload_asset(second_client)
    await _insert_asset(b_id, kind="snapshot", source="tool")
    await _insert_asset(b_id, kind="export", source="tool", library_hidden=True)
    # 时间窗口：下界命中、上界恰好不包含（PRD §8.2）
    lo_id = await _insert_asset(a_id, created_at=datetime(2020, 3, 1, tzinfo=UTC))
    boundary_id = await _insert_asset(a_id, created_at=datetime(2020, 6, 1, tzinfo=UTC))

    common = {"page_size": 100}

    by_user = await admin_client.get("/api/admin/assets", params=common | {"user_id": a_id})
    assert by_user.status_code == 200, by_user.text
    body = by_user.json()
    expected = await _asset_ids("user_id = :u", u=uuid.UUID(a_id))
    assert [item["id"] for item in body["items"]] == expected
    assert body["total"] == len(expected)  # total 为同口径真实总数，JOIN 不抬高
    assert body["page"] == 1 and body["page_size"] == 100
    assert all(item["user_id"] == a_id for item in body["items"])
    assert all(item["username"] == other_credentials["username"] for item in body["items"])
    assert all("storage_key" not in item for item in body["items"])

    by_kind = await admin_client.get("/api/admin/assets", params=common | {"kind": "mask"})
    assert by_kind.status_code == 200, by_kind.text
    expected = await _asset_ids("kind = 'mask'")
    assert [item["id"] for item in by_kind.json()["items"]] == expected
    assert mask_id in expected  # 跨用户全量：不限于单个用户

    by_source = await admin_client.get(
        "/api/admin/assets", params=common | {"source": "upload"}
    )
    assert by_source.status_code == 200, by_source.text
    expected = await _asset_ids("source = 'upload'")
    assert [item["id"] for item in by_source.json()["items"]] == expected

    by_hidden = await admin_client.get(
        "/api/admin/assets", params=common | {"library_hidden": "true"}
    )
    assert by_hidden.status_code == 200, by_hidden.text
    expected = await _asset_ids("library_hidden")
    assert [item["id"] for item in by_hidden.json()["items"]] == expected
    assert all(item["library_hidden"] is True for item in by_hidden.json()["items"])

    combined = await admin_client.get(
        "/api/admin/assets", params=common | {"user_id": a_id, "kind": "generated"}
    )
    assert combined.status_code == 200, combined.text
    expected = await _asset_ids("user_id = :u AND kind = 'generated'", u=uuid.UUID(a_id))
    assert [item["id"] for item in combined.json()["items"]] == expected
    assert generated_id in expected

    window = {"created_from": "2020-03-01T00:00:00+00:00",
              "created_to": "2020-06-01T00:00:00+00:00"}
    by_range = await admin_client.get("/api/admin/assets", params=common | window)
    assert by_range.status_code == 200, by_range.text
    expected = await _asset_ids(
        "created_at >= :lo AND created_at < :hi",
        lo=datetime(2020, 3, 1, tzinfo=UTC),
        hi=datetime(2020, 6, 1, tzinfo=UTC),
    )
    assert [item["id"] for item in by_range.json()["items"]] == expected
    assert lo_id in expected
    assert boundary_id not in expected  # 上界不包含
    assert b_upload not in expected

    visible_only = await admin_client.get(
        "/api/admin/assets",
        params=common | {"library_hidden": "false", "user_id": b_id},
    )
    assert visible_only.status_code == 200, visible_only.text
    expected = await _asset_ids("user_id = :u AND NOT library_hidden", u=uuid.UUID(b_id))
    assert [item["id"] for item in visible_only.json()["items"]] == expected


async def test_admin_asset_list_q_matches_title_substring_escaping_or_full_uuid(
    admin_client, client, second_client, credentials, other_credentials,
    third_credentials,
):
    """q 命中 title 包含（通配符转义）或完整素材 UUID（矩阵行 1 / ADM-AC-035）。

    转义用例：另一素材名为 "abc"——若实现未转义下划线，q="a_c" 会被当作
    通配符同时命中 "abc"，断言随之失败。
    """
    await _make_admin(admin_client, credentials)
    await _register(client, other_credentials)
    await _register(second_client, third_credentials)

    underscore = await upload_asset(client)
    plain = await upload_asset(client)
    other_user_asset = await upload_asset(second_client)
    renamed = await client.patch(f"/api/assets/{underscore}", json={"title": "序号_a_c"})
    assert renamed.status_code == 200, renamed.text
    renamed = await client.patch(f"/api/assets/{plain}", json={"title": "普通命名"})
    assert renamed.status_code == 200, renamed.text

    common = {"page_size": 100}

    by_sub = await admin_client.get("/api/admin/assets", params=common | {"q": "a_c"})
    assert by_sub.status_code == 200, by_sub.text
    expected = await _asset_ids("title LIKE :pat ESCAPE '@'", pat="%a@_c%")
    assert [item["id"] for item in by_sub.json()["items"]] == expected
    assert underscore in expected
    assert other_user_asset not in expected  # 未转义时会因 _ 通配误命中

    by_title = await admin_client.get("/api/admin/assets", params=common | {"q": "普通"})
    assert by_title.status_code == 200, by_title.text
    assert [item["id"] for item in by_title.json()["items"]] == [plain]

    by_uuid = await admin_client.get(
        "/api/admin/assets", params=common | {"q": other_user_asset}
    )
    assert by_uuid.status_code == 200, by_uuid.text
    assert [item["id"] for item in by_uuid.json()["items"]] == [other_user_asset]

    # 用户审查反馈（2026-10-26，PRD §8.2 修订）：q 也命中归属用户名包含
    marker_username = f"test_uname_{uuid.uuid4().hex[:10]}"
    registered = await client.post(
        "/api/auth/register",
        json={"username": marker_username, "password": "pass-word-123"},
    )
    assert registered.status_code == 201, registered.text  # 注册即登录，client 归属切换
    marker_asset = await upload_asset(client)
    frag = marker_username[5:21]
    by_owner = await admin_client.get("/api/admin/assets", params=common | {"q": frag})
    assert by_owner.status_code == 200, by_owner.text
    expected_owner = await _asset_ids(
        "user_id IN (SELECT id FROM users WHERE username LIKE :pat ESCAPE '@')",
        pat=f"%{frag}%",
    )
    assert [item["id"] for item in by_owner.json()["items"]] == expected_owner
    assert marker_asset in expected_owner


async def test_admin_asset_list_sort_whitelist_title_nulls_and_pagination(
    admin_client, client, credentials, other_credentials
):
    """排序白名单 created_at/size_bytes/title；title 排序时 null 行在两个方向都
    稳定尾随（WI 决策 1「实现自定但须稳定」）；created_at 并列按 id 同方向稳定；
    分页切片正确；白名单外 sort、非法 order、无时区与反向时间区间 422
    （矩阵行 1 / ADM-AC-035）。"""
    await _make_admin(admin_client, credentials)
    a_id = await _register(client, other_credentials)

    first = await upload_asset(client)
    second = await upload_asset(client)
    third = await upload_asset(client)
    fourth = await upload_asset(client)
    for asset_id, title in ((first, "乙标题"), (second, "甲标题")):
        renamed = await client.patch(f"/api/assets/{asset_id}", json={"title": title})
        assert renamed.status_code == 200, renamed.text
    await _insert_asset(a_id, size_bytes=900)
    await _insert_asset(a_id, size_bytes=100)
    tie_ids = [
        await _insert_asset(a_id, created_at=datetime(2020, 1, 1, tzinfo=UTC))
        for _ in range(3)
    ]
    scope = "user_id = :u"
    params = {"u": uuid.UUID(a_id)}

    # size_bytes：契约口径 oracle（主排序 + id 同方向）
    by_size = await admin_client.get(
        "/api/admin/assets",
        params={"sort": "size_bytes", "order": "desc", "user_id": a_id, "page_size": 100},
    )
    assert by_size.status_code == 200, by_size.text
    async with SessionFactory() as session:
        rows = await session.execute(
            text(f"SELECT id::text FROM assets WHERE {scope}"
                 " ORDER BY size_bytes DESC, id DESC"),
            params,
        )
        expected = rows.scalars().all()
    assert [item["id"] for item in by_size.json()["items"]] == expected

    # title 排序：有名列在前（按 title 方向），null 行两个方向都尾随且次序稳定
    all_ids = await _asset_ids(scope, **params)
    assert len(all_ids) == 9  # 造数前提自检：4 上传 + 2 size 直插 + 3 并列直插
    assert {first, second, third, fourth} <= set(all_ids)
    for order in ("asc", "desc"):
        response = await admin_client.get(
            "/api/admin/assets",
            params={"sort": "title", "order": order, "user_id": a_id, "page_size": 100},
        )
        assert response.status_code == 200, response.text
        ids = [item["id"] for item in response.json()["items"]]
        # user_id 筛选下该用户库存即全量（完整性，与排序口径无关；原列表全等
        # 写法与本用例 :498 的 title 序前缀 oracle 互斥，经授权改为多重集比较）
        assert sorted(ids) == sorted(all_ids)
        async with SessionFactory() as session:
            rows = await session.execute(
                text(f"SELECT id::text FROM assets WHERE {scope} AND title IS NOT NULL"
                     f" ORDER BY title {order}, id {order}"),
                params,
            )
            titled = rows.scalars().all()
        assert ids[: len(titled)] == titled
        trailing = ids[len(titled):]
        assert set(trailing) == set(all_ids) - set(titled)
        assert all(
            item["title"] is None
            for item in response.json()["items"][len(titled):]
        )
        # 稳定性：同参数两次请求次序完全一致
        again = await admin_client.get(
            "/api/admin/assets",
            params={"sort": "title", "order": order, "user_id": a_id, "page_size": 100},
        )
        assert [item["id"] for item in again.json()["items"]] == ids

    # created_at 并列（2020 年的三条直插素材并列且最旧）：asc 时前三位按 id 升序
    ascending = await admin_client.get(
        "/api/admin/assets",
        params={"sort": "created_at", "order": "asc", "user_id": a_id, "page_size": 100},
    )
    assert [item["id"] for item in ascending.json()["items"]][:3] == sorted(tie_ids)

    # 分页切片：created_at desc 契约口径
    expected = await _asset_ids(scope, **params)
    for page in (1, 2, 3):
        sliced = await admin_client.get(
            "/api/admin/assets",
            params={"sort": "created_at", "order": "desc", "user_id": a_id,
                    "page": page, "page_size": 3},
        )
        assert sliced.status_code == 200, sliced.text
        body = sliced.json()
        assert [item["id"] for item in body["items"]] == expected[(page - 1) * 3: page * 3]
        assert body["total"] == len(expected)

    # 参数边界：白名单外 sort、非法 order、无时区时间、反向区间一律 422
    for bad in (
        {"sort": "kind"},
        {"sort": "created_at", "order": "sideways"},
        {"created_from": "2026-01-01T00:00:00"},
        {"created_from": "2026-06-01T00:00:00+00:00",
         "created_to": "2026-01-01T00:00:00+00:00"},
    ):
        response = await admin_client.get("/api/admin/assets", params=bad)
        assert response.status_code == 422, f"{bad}: {response.text}"


# ---- 矩阵行 1（ADM-FR-017 / WI-AC-002=ADM-AC-036）：详情全量引用 ----


async def test_admin_asset_detail_full_references_match_constructed_topology(
    admin_client, client, credentials, other_credentials
):
    """详情返回完整引用（references 风格行：session_id/title/types），覆盖原图、
    当前图、图层（当前 document）、仅历史、多会话墙与无引用对照；列表项与详情
    逐字段一致；引用计数摘要存在且与引用有无一致（矩阵行 1 / ADM-AC-036）。"""
    await _make_admin(admin_client, credentials)
    a_id = await _register(client, other_credentials)

    star = await upload_asset(client)
    # S1：star 先为原图+当前图，切换后只剩原图（+保留历史）引用
    s1 = await open_session(client, current_asset_id=star, title="第一会话")
    successor = await upload_asset(client)
    switched = await client.patch(
        f"/api/sessions/{s1['id']}", json={"current_asset_id": successor}
    )
    assert switched.status_code == 200, switched.text
    # S2：star 同时是第二会话的原图与当前图（当前图自动进墙 → 多会话墙之一）
    s2 = await open_session(client, current_asset_id=star, title="第二会话")
    # S3：当前 document 图层引用
    s3 = await open_session(client, title="图层会话")
    await _add_document_layer(s3["id"], star)
    # S4：仅图片墙关联
    s4 = await open_session(client, title="墙会话", asset_ids=[star])
    # S5：仅保留历史引用（两条，每条恰好一个引用文档）
    s5 = await open_session(client, title="历史会话")
    await _add_history_reference(a_id, s5["id"], 2, star)
    await _add_history_reference(a_id, s5["id"], 3, star)
    # 无引用对照
    bare = await upload_asset(client)

    detail = await admin_client.get(f"/api/admin/assets/{star}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["id"] == star
    assert body["user_id"] == a_id
    assert body["username"] == other_credentials["username"]
    assert body["kind"] == "original"
    assert body["library_hidden"] is False
    assert "storage_key" not in body
    assert body["can_rename"] is True
    assert body["can_delete"] is False  # 原图/当前图/图层引用仍阻断
    assert "asset_in_use" in detail.text  # 原因码随能力返回

    references = _types_by_session(body["references"])
    assert s1["id"] in references and "original" in references[s1["id"]]
    assert s2["id"] in references
    assert {"original", "current"} <= references[s2["id"]]
    assert s3["id"] in references and "layer" in references[s3["id"]]
    assert s5["id"] in references and "history" in references[s5["id"]]
    [s1_row] = [row for row in body["references"] if row["session_id"] == s1["id"]]
    assert s1_row["title"] == "第一会话"
    # 引用计数摘要：各类型计数之和与引用有无一致
    assert sum(body["references_count"].values()) > 0

    bare_detail = await admin_client.get(f"/api/admin/assets/{bare}")
    assert bare_detail.status_code == 200, bare_detail.text
    bare_body = bare_detail.json()
    assert bare_body["references"] == []
    assert sum(bare_body["references_count"].values()) == 0
    assert bare_body["can_delete"] is True
    assert bare_body["can_rename"] is True

    # 列表项与详情逐字段一致（签名 URL 按调用时刻生成，不要求逐字节相同）
    listed = await admin_client.get(
        "/api/admin/assets", params={"q": star, "page_size": 100}
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    [item] = listed.json()["items"]
    for key, value in item.items():
        if key == "preview_url":
            assert "X-Amz-Signature" in body[key]
        else:
            assert body[key] == value, f"{key}: 列表与详情不一致"

    # 造数前提自检：多会话墙锚点确实成立（S2 当前图 + S4 显式墙关联）
    assert await _wall_count(star) >= 1
    assert s4["id"] != s2["id"]


# ---- 矩阵行 1（ADM-FR-017 / WI-AC-002=ADM-AC-036）：签名 URL 与读取不留痕 ----


async def test_admin_asset_preview_url_fetchable_renewable_and_never_audited(
    admin_client, client, credentials, other_credentials
):
    """列表与详情的 preview_url 为短期签名 URL，可直接取回对象且可续签（重取
    仍有效）；普通读取（列表/详情）不产生审计行，任何审计 summary 不含签名
    URL 痕迹（矩阵行 1 / ADM-AC-036，WI 决策 1：preview_url 不入审计）。"""
    admin = await _make_admin(admin_client, credentials)
    a_id = await _register(client, other_credentials)
    asset_id = await upload_asset(client)
    key = await storage_key_of(asset_id)

    before = await _audit_count(admin["id"])
    listed = await admin_client.get(
        "/api/admin/assets", params={"user_id": a_id, "page_size": 100}
    )
    assert listed.status_code == 200, listed.text
    [item] = listed.json()["items"]
    assert item["id"] == asset_id
    assert "X-Amz-Signature" in item["preview_url"]

    detail = await admin_client.get(f"/api/admin/assets/{asset_id}")
    assert detail.status_code == 200, detail.text
    assert "X-Amz-Signature" in detail.json()["preview_url"]

    # 签名 URL 可直接取回对象本身（可用性）
    async with httpx.AsyncClient() as raw:
        fetched = await raw.get(item["preview_url"])
    assert fetched.status_code == 200, fetched.text
    assert fetched.content == await storage.get(key)
    # 续签：重取详情后再次拿到的签名 URL 仍然可用
    renewed = await admin_client.get(f"/api/admin/assets/{asset_id}")
    assert renewed.status_code == 200, renewed.text
    async with httpx.AsyncClient() as raw:
        refetched = await raw.get(renewed.json()["preview_url"])
    assert refetched.status_code == 200, refetched.text

    # 读取零审计，审计 summary 永无签名 URL（白名单无此键的天然保证）
    assert await _audit_count(admin["id"]) == before
    for row in await _audit_rows(admin["id"]):
        assert "X-Amz" not in row["summary"]
        assert "http" not in row["summary"].lower()


async def test_asset_references_service_reports_topology_and_active_tasks(
    client, credentials
):
    """新服务函数 asset_references(session, asset)（WI 决策 2）按构造拓扑如实
    返回 original_of/current_of/layer_in(历史文档数)/wall_sessions/active_tasks；
    扫描与用户端同口径且全量、不受页面过滤限制（矩阵行 1 / ADM-AC-036）。

    惰性导入：函数尚不存在时本用例以 ImportError 呈现 Red。
    """
    from app.services.assets import asset_references  # Red 阶段惰性导入

    a_id = await _register(client, credentials)
    star = await upload_asset(client)
    s1 = await open_session(client, current_asset_id=star)  # 原图+当前图+墙
    await open_session(client, asset_ids=[star])  # 第二面墙
    s3 = await open_session(client)
    await _add_history_reference(a_id, s3["id"], 2, star)
    await _add_history_reference(a_id, s3["id"], 3, star)

    async with SessionFactory() as session:
        asset = await session.get(Asset, uuid.UUID(star))
        references = await asset_references(session, asset)
    for key in ("original_of", "current_of", "layer_in", "wall_sessions", "active_tasks"):
        assert key in references, key
    assert set(references["original_of"]) == {s1["id"]}
    assert set(references["current_of"]) == {s1["id"]}
    assert references["layer_in"] == 2  # 两条保留历史，各恰好一个引用文档
    assert len(references["wall_sessions"]) >= 1  # 当前图锚点 + 显式墙关联
    assert references["active_tasks"] is False

    # 同 owner 出现 queued 任务后 active_tasks 翻转（mock provider 下无人消费）
    generation = await client.post(
        "/api/generations",
        json={"prompt": "浅木色桌面上的白色马克杯", "ratio": "1:1", "count": 1},
    )
    assert generation.status_code == 202, generation.text
    async with SessionFactory() as session:
        asset = await session.get(Asset, uuid.UUID(star))
        references = await asset_references(session, asset)
    assert references["active_tasks"] is True


# ---- 端点级 401/403（矩阵行 1 附属，与阶段 2 同款闭合）----


@pytest.mark.parametrize(("method", "path", "body"), _probe_list(), ids=_PROBE_IDS)
async def test_admin_asset_endpoints_require_authentication(method, path, body):
    """全部新素材端点未登录一律 401（矩阵行 1 附属 / ADM-AC-003 同口径）。"""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as anonymous:
        response = await anonymous.request(method, path, json=body)
    assert response.status_code == 401, response.text


@pytest.mark.parametrize(("method", "path", "body"), _probe_list(), ids=_PROBE_IDS)
async def test_admin_asset_endpoints_reject_non_admin_users(
    client, credentials, method, path, body
):
    """全部新素材端点对普通用户一律 403（矩阵行 1 附属 / ADM-AC-003 同口径）。"""
    await client.post("/api/auth/register", json=credentials)

    response = await client.request(method, path, json=body)

    assert response.status_code == 403, response.text


# ---- 矩阵行 2（ADM-FR-018 / WI-AC-003=ADM-AC-037）：title 规则 ----


async def test_admin_rename_follows_user_title_rules(
    admin_client, client, credentials, other_credentials
):
    """管理员重命名沿用 title 规则：trim、空白归一 null、null 合法、80 码点合法
    （矩阵行 2 / ADM-AC-037，PRD §6.5）。"""
    await _make_admin(admin_client, credentials)
    await _register(client, other_credentials)
    asset_id = await upload_asset(client)

    trimmed = await admin_client.patch(
        f"/api/admin/assets/{asset_id}", json={"title": "  管理员命名  "}
    )
    assert trimmed.status_code == 200, trimmed.text
    assert trimmed.json()["title"] == "管理员命名"
    detail = await admin_client.get(f"/api/admin/assets/{asset_id}")
    assert detail.json()["title"] == "管理员命名"

    blanked = await admin_client.patch(
        f"/api/admin/assets/{asset_id}", json={"title": "   "}
    )
    assert blanked.status_code == 200, blanked.text
    assert blanked.json()["title"] is None

    nulled = await admin_client.patch(
        f"/api/admin/assets/{asset_id}", json={"title": None}
    )
    assert nulled.status_code == 200, nulled.text
    assert nulled.json()["title"] is None

    for title in ("汉" * 80, "😀" * 80):
        ok = await admin_client.patch(
            f"/api/admin/assets/{asset_id}", json={"title": title}
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["title"] == title


@pytest.mark.parametrize(
    "payload",
    [{"title": "汉" * 81}, {"title": "😀" * 81}, {}, {"title": 3}, {"title": ["x"]}],
    ids=["han-over", "emoji-over", "missing", "non-string", "list"],
)
async def test_admin_rename_rejects_invalid_payloads(
    admin_client, client, credentials, other_credentials, payload
):
    """超 80 码点与非字符串/缺失字段一律 422，零写入（矩阵行 2 / ADM-AC-037）。"""
    await _make_admin(admin_client, credentials)
    await _register(client, other_credentials)
    asset_id = await upload_asset(client)

    response = await admin_client.patch(f"/api/admin/assets/{asset_id}", json=payload)

    assert response.status_code == 422, response.text
    row = await _asset_row(asset_id)
    assert row is not None and row["title"] is None  # 超长不截断、非法不写入


# ---- 矩阵行 2（ADM-FR-018 / WI-AC-003=ADM-AC-037）：类型边界 ----


async def test_admin_can_rename_snapshot_but_not_delete_it(
    admin_client, client, credentials, other_credentials
):
    """快照（SNAPSHOT）可命名（FR-018）但不可单删：409 unsupported_asset_kind，
    can_delete=false 且原因码随详情返回（矩阵行 2 / ADM-AC-037，WI 决策 3/6）。"""
    await _make_admin(admin_client, credentials)
    await _register(client, other_credentials)
    session = await open_session(client)
    snapshot_id = await seed_wall_asset(client, session["id"], "snapshot", 1, "snap")

    detail = await admin_client.get(f"/api/admin/assets/{snapshot_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["kind"] == "snapshot"
    assert body["can_rename"] is True
    assert body["can_delete"] is False
    assert "unsupported_asset_kind" in detail.text

    renamed = await admin_client.patch(
        f"/api/admin/assets/{snapshot_id}", json={"title": "定稿快照"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "定稿快照"

    deleted = await admin_client.delete(f"/api/admin/assets/{snapshot_id}")
    assert deleted.status_code == 409, deleted.text
    assert deleted.json()["detail"]["code"] == "unsupported_asset_kind"
    assert (await _asset_row(snapshot_id)) is not None  # 零写入


async def test_admin_rename_hidden_asset_conflicts_with_stable_code(
    admin_client, client, credentials, other_credentials
):
    """隐藏素材管理员仍可见（列表 library_hidden=true 与详情 200），但只读：
    重命名 409 asset_readonly（WI 决策 3 固化码）、单删 404（delete_for_user
    对隐藏行不暴露管理入口）、can_rename=false（矩阵行 2 / ADM-AC-037）。"""
    await _make_admin(admin_client, credentials)
    a_id = await _register(client, other_credentials)
    hidden_id = await _insert_asset(a_id, library_hidden=True)

    listed = await admin_client.get(
        "/api/admin/assets", params={"library_hidden": "true", "page_size": 100}
    )
    assert listed.status_code == 200, listed.text
    assert hidden_id in [item["id"] for item in listed.json()["items"]]

    detail = await admin_client.get(f"/api/admin/assets/{hidden_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["library_hidden"] is True
    assert body["can_rename"] is False
    assert body["can_delete"] is False
    assert "asset_readonly" in detail.text

    renamed = await admin_client.patch(
        f"/api/admin/assets/{hidden_id}", json={"title": "改名尝试"}
    )
    assert renamed.status_code == 409, renamed.text
    assert renamed.json()["detail"]["code"] == "asset_readonly"
    assert (await _asset_row(hidden_id))["title"] is None  # 零写入

    deleted = await admin_client.delete(f"/api/admin/assets/{hidden_id}")
    assert deleted.status_code == 404, deleted.text
    assert deleted.json()["detail"]["code"] == "not_found"
    assert (await _asset_row(hidden_id)) is not None

    # 造数前提自检：隐藏素材对属主同样不可见（既有用户端语义未被破坏）
    owner_view = await client.get(f"/api/assets/{hidden_id}")
    assert owner_view.status_code == 404


async def test_admin_rename_blocked_while_owner_deleting(
    admin_client, client, credentials, other_credentials
):
    """目标用户 deleting 时素材只读：重命名 409 account_deleting（WI 决策 3）、
    can_rename=false（矩阵行 2 / ADM-AC-037）。"""
    await _make_admin(admin_client, credentials)
    a_id = await _register(client, other_credentials)
    asset_id = await upload_asset(client)
    async with SessionFactory() as session:
        await session.execute(
            text("UPDATE users SET status = 'deleting' WHERE id = :u"),
            {"u": uuid.UUID(a_id)},
        )
        await session.commit()

    detail = await admin_client.get(f"/api/admin/assets/{asset_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["can_rename"] is False
    assert "account_deleting" in detail.text

    renamed = await admin_client.patch(
        f"/api/admin/assets/{asset_id}", json={"title": "冻结期改名"}
    )
    assert renamed.status_code == 409, renamed.text
    assert renamed.json()["detail"]["code"] == "account_deleting"
    assert (await _asset_row(asset_id))["title"] is None  # 零写入


async def test_admin_internal_derived_kind_rename_allowed_delete_rejected(
    admin_client, client, credentials, other_credentials
):
    """内部派生 kind（MASK）可命名但不因管理员身份获得删除权：409
    unsupported_asset_kind，不绕过 UNMANAGEABLE_KINDS（矩阵行 2 / ADM-AC-037，
    WI 决策 3/6）。"""
    await _make_admin(admin_client, credentials)
    await _register(client, other_credentials)
    session = await open_session(client)
    mask_id = await create_mask(client, session)

    renamed = await admin_client.patch(
        f"/api/admin/assets/{mask_id}", json={"title": "内部遮罩"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "内部遮罩"

    detail = await admin_client.get(f"/api/admin/assets/{mask_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["can_delete"] is False
    assert "unsupported_asset_kind" in detail.text

    deleted = await admin_client.delete(f"/api/admin/assets/{mask_id}")
    assert deleted.status_code == 409, deleted.text
    assert deleted.json()["detail"]["code"] == "unsupported_asset_kind"
    assert (await _asset_row(mask_id)) is not None  # 零写入


# ---- 矩阵行 3（ADM-FR-019 / WI-AC-004=ADM-AC-038）：单删保护 ----


async def test_admin_single_delete_conflicts_carry_stable_reasons(
    admin_client, client, credentials, other_credentials
):
    """被原图/当前图/图层引用或同 owner 活动任务的素材，管理员单删 409 且带
    稳定原因码与 references 风格详情；DB 不变（矩阵行 3 / ADM-AC-038）。"""
    await _make_admin(admin_client, credentials)
    await _register(client, other_credentials)

    holder = await open_session(client, title="占位会话")
    original = holder["current_asset_id"]
    successor = await upload_asset(client)
    switched = await client.patch(
        f"/api/sessions/{holder['id']}", json={"current_asset_id": successor}
    )
    assert switched.status_code == 200, switched.text

    # 原图引用（切换当前图后原图仍被 original 引用）
    response = await admin_client.delete(f"/api/admin/assets/{original}")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "asset_in_use"
    assert detail["message"]
    [reference] = detail["references"]
    assert reference["session_id"] == holder["id"]
    assert reference["title"] == "占位会话"
    assert "original" in reference["types"]
    assert (await _asset_row(original)) is not None

    # 当前图引用
    response = await admin_client.delete(f"/api/admin/assets/{successor}")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "asset_in_use"
    [reference] = detail["references"]
    assert reference["session_id"] == holder["id"]
    assert "current" in reference["types"]

    # 当前 document 图层引用
    layer_asset = await upload_asset(client)
    layer_session = await open_session(client, title="图层会话")
    await _add_document_layer(layer_session["id"], layer_asset)
    response = await admin_client.delete(f"/api/admin/assets/{layer_asset}")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "asset_in_use"
    assert "layer" in detail["references"][0]["types"]
    assert detail["references"][0]["session_id"] == layer_session["id"]

    # 同 owner 活动任务阻断（对无引用素材同样生效）
    fresh = await upload_asset(client)
    generation = await client.post(
        "/api/generations",
        json={"prompt": "浅木色桌面上的白色马克杯", "ratio": "1:1", "count": 1},
    )
    assert generation.status_code == 202, generation.text
    response = await admin_client.delete(f"/api/admin/assets/{fresh}")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "active_tasks"
    assert (await _asset_row(fresh)) is not None

    # 不存在与非存在 ID 统一 404（WI 决策 4）
    missing = await admin_client.delete(f"/api/admin/assets/{uuid.uuid4()}")
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["code"] == "not_found"


async def test_admin_single_delete_history_only_hides_and_preserves_undo_redo(
    admin_client, client, credentials, other_credentials
):
    """仅历史引用的素材由管理员单删转隐藏式删除：行与对象保留、墙关联清空、
    创意库/图片墙不可见；属主撤销把该素材恢复为当前图并随详情带出，重做恢复
    正常（矩阵行 3 / ADM-AC-039，复用既有隐藏删除语义断言）。"""
    admin = await _make_admin(admin_client, credentials)
    await _register(client, other_credentials)
    session = await open_session(client)
    middle = (await apply(client, session["id"], "adjust_image", {"brightness": 0.1}))[
        "current_asset_id"
    ]
    latest = (await apply(client, session["id"], "adjust_image", {"brightness": 0.2}))[
        "current_asset_id"
    ]
    assert middle != session["original_asset_id"] != latest
    middle_key = await storage_key_of(middle)

    response = await admin_client.delete(f"/api/admin/assets/{middle}")

    # 两态之 hidden：HTTP 形状实现自定（WI 决策 4），以 DB/对象语义锚定
    assert response.status_code in (200, 204), response.text
    row = await _asset_row(middle)
    assert row is not None and row["library_hidden"] is True
    assert await _wall_count(middle) == 0
    assert await storage.get(middle_key)  # 对象保留供撤销/重做
    assert (await client.get(f"/api/assets/{middle}")).status_code == 404
    library = (await client.get("/api/assets/library")).json()
    assert middle not in [asset["id"] for group in library for asset in group["assets"]]
    session_detail = (await client.get(f"/api/sessions/{session['id']}")).json()
    assert middle not in [asset["id"] for asset in session_detail["assets"]]

    # 撤销恢复 middle 为当前图：活跃状态并集必须重新带出该素材
    undone = (await client.post(f"/api/sessions/{session['id']}/undo")).json()
    assert undone["current_asset_id"] == middle
    assert middle in [asset["id"] for asset in undone["assets"]]
    # 重做回 latest，middle 重新不可见
    redone = (await client.post(f"/api/sessions/{session['id']}/redo")).json()
    assert redone["current_asset_id"] == latest
    assert middle not in [asset["id"] for asset in redone["assets"]]

    # 管理员视角：隐藏行仍可见、只读
    detail = await admin_client.get(f"/api/admin/assets/{middle}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["library_hidden"] is True
    assert detail.json()["can_rename"] is False

    # 隐藏式删除同样属于素材删除动作，须留痕（WI 决策 4）
    rows = await _audit_rows(admin["id"], middle)
    assert ("asset_delete", "success") in [(r["action"], r["result"]) for r in rows]


async def test_admin_single_delete_unreferenced_succeeds_despite_object_cleanup_failure(
    admin_client, client, credentials, other_credentials, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """无引用素材管理员单删成功（仅墙关联同样放行并级联清理）；对象清理失败只
    告警不回滚业务删除，warning 含 storage_key 可定位（矩阵行 3 / ADM-AC-039，
    monkeypatch storage 手法同 test_assets）。"""

    async def broken_delete(key: str) -> None:
        raise RuntimeError("s3 down")

    monkeypatch.setattr("app.storage.delete", broken_delete)
    await _make_admin(admin_client, credentials)
    await _register(client, other_credentials)
    session = await open_session(client)
    wall_only = await upload_asset(client)
    await attach_to_wall(session["id"], wall_only, 1)

    detail = await admin_client.get(f"/api/admin/assets/{wall_only}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["can_delete"] is True  # 仅墙关联不构成保护

    key = await storage_key_of(wall_only)
    with caplog.at_level(logging.WARNING):
        response = await admin_client.delete(f"/api/admin/assets/{wall_only}")

    # 两态之 deleted：HTTP 形状实现自定（WI 决策 4），以 DB 语义锚定
    assert response.status_code in (200, 204), response.text
    assert (await _asset_row(wall_only)) is None
    assert await _wall_count(wall_only) == 0  # 墙关联级联移除
    assert (await client.get(f"/api/assets/{wall_only}")).status_code == 404
    # 对象清理失败如实告警且对象残留（既有「删除成功+清理告警」语义，不误报回收）
    assert any(
        key in record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )
    assert await storage.get(key)  # 清理失败不伪装回收


# ---- 矩阵行 4（ADM-FR-020 / WI-AC-005=ADM-AC-040）：批删参数与整批核验 ----


async def test_admin_batch_delete_rejects_invalid_payloads_with_zero_write(
    admin_client, client, credentials, other_credentials
):
    """空数组、超量（>100）、非法 UUID、缺字段整批 422，零写入（矩阵行 4 /
    ADM-AC-040）。"""
    await _make_admin(admin_client, credentials)
    await _register(client, other_credentials)
    asset_id = await upload_asset(client)

    empty = await admin_client.post(
        "/api/admin/assets/batch-delete", json={"asset_ids": []}
    )
    assert empty.status_code == 422, empty.text

    over = await admin_client.post(
        "/api/admin/assets/batch-delete", json={"asset_ids": [asset_id] * 101}
    )
    assert over.status_code == 422, over.text

    invalid = await admin_client.post(
        "/api/admin/assets/batch-delete", json={"asset_ids": [asset_id, "not-a-uuid"]}
    )
    assert invalid.status_code == 422, invalid.text

    missing_field = await admin_client.post("/api/admin/assets/batch-delete", json={})
    assert missing_field.status_code == 422, missing_field.text

    assert (await _asset_row(asset_id)) is not None  # 零写入


async def test_admin_batch_delete_missing_id_fails_whole_batch_with_zero_write(
    admin_client, client, second_client, credentials, other_credentials,
    third_credentials,
):
    """跨用户存在性核验：任一 ID 缺失 → 整批 404 零写入；不回显具体 ID；全部
    存在时跨用户归属本身不触发 404（矩阵行 4 / ADM-AC-040，WI 决策 5）。"""
    await _make_admin(admin_client, credentials)
    await _register(client, other_credentials)
    await _register(second_client, third_credentials)
    own = await upload_asset(client)
    foreign = await upload_asset(second_client)
    missing = str(uuid.uuid4())

    response = await admin_client.post(
        "/api/admin/assets/batch-delete",
        json={"asset_ids": [own, foreign, missing]},
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "not_found"
    for leaked in (own, foreign, missing):
        assert leaked not in response.text  # 不回显具体缺失 ID
    # 零写入：合法项也未被删除
    assert (await _asset_row(own)) is not None
    assert (await _asset_row(foreign)) is not None


async def test_admin_batch_delete_dedupes_and_applies_per_owner_protection(
    admin_client, client, second_client, credentials, other_credentials,
    third_credentials,
):
    """去重保序（每 ID 至多出现一次）；逐项按各自 owner 独立重算保护：被引用与
    内部派生项 skipped（稳定 code + 中文 reason），其余跨用户删除；deleted 与
    skipped 不重叠且并集覆盖去重后输入（矩阵行 4 / ADM-AC-041，WI 决策 5）。"""
    await _make_admin(admin_client, credentials)
    await _register(client, other_credentials)
    await _register(second_client, third_credentials)

    a_first = await upload_asset(client)
    a_second = await upload_asset(client)
    b_loose = await upload_asset(second_client)
    b_session = await open_session(second_client, title="乙的会话")
    b_referenced = b_session["current_asset_id"]
    b_mask = await create_mask(second_client, b_session)

    response = await admin_client.post(
        "/api/admin/assets/batch-delete",
        json={"asset_ids": [a_first, b_loose, a_first, b_referenced, b_mask, a_second]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] == [a_first, b_loose, a_second]  # 去重保序
    assert [item["id"] for item in body["skipped"]] == [b_referenced, b_mask]
    [ref_item] = [item for item in body["skipped"] if item["id"] == b_referenced]
    [mask_item] = [item for item in body["skipped"] if item["id"] == b_mask]
    assert ref_item["code"] == "asset_in_use"
    assert mask_item["code"] == "unsupported_asset_kind"
    for item in body["skipped"]:
        assert isinstance(item["reason"], str) and item["reason"]
        assert re.search(r"[\u4e00-\u9fff]", item["reason"])  # 中文 reason
    # 不重叠且覆盖去重后输入
    deleted_set = set(body["deleted"])
    skipped_set = {item["id"] for item in body["skipped"]}
    assert not deleted_set & skipped_set
    assert deleted_set | skipped_set == {
        a_first, b_loose, a_second, b_referenced, b_mask,
    }
    # 逐项按 owner 保护：跨用户合法项已删，受保护项保留
    assert (await _asset_row(a_first)) is None
    assert (await _asset_row(a_second)) is None
    assert (await _asset_row(b_loose)) is None
    assert (await _asset_row(b_referenced)) is not None
    assert (await _asset_row(b_mask)) is not None


async def test_admin_batch_delete_abrupt_error_stops_with_committed_items_kept(
    admin_client, client, second_client, credentials, other_credentials,
    third_credentials, monkeypatch: pytest.MonkeyPatch,
):
    """突发异常停止后续项并整体 500：先期已提交项不回滚，未尝试项保持原状，
    响应附部分结果说明（已删 ID 可见）（矩阵行 4 / ADM-AC-041，WI 决策 5）。

    注入方式：对第二个 owner 的 has_active_tasks 注入 RuntimeError（首个 owner
    的项已独立事务提交）。已删项 ID 须出现在响应文本中。
    """
    await _make_admin(admin_client, credentials)
    await _register(client, other_credentials)
    b_id = await _register(second_client, third_credentials)
    a_first = await upload_asset(client)
    b_asset = await upload_asset(second_client)
    a_second = await upload_asset(client)

    real_has_active_tasks = runs.has_active_tasks

    async def raising_for_b(session, user_id):
        if str(user_id) == b_id:
            raise RuntimeError("突发故障注入")
        return await real_has_active_tasks(session, user_id)

    monkeypatch.setattr("app.services.runs.has_active_tasks", raising_for_b)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as lenient:
        lenient.cookies.update(admin_client.cookies)
        response = await lenient.post(
            "/api/admin/assets/batch-delete",
            json={"asset_ids": [a_first, b_asset, a_second]},
        )

    assert response.status_code == 500, response.text
    assert a_first in response.text  # 响应附部分结果说明（WI 决策 5）
    # 已提交项不回滚，未尝试项原状
    assert (await _asset_row(a_first)) is None
    assert (await _asset_row(b_asset)) is not None
    assert (await _asset_row(a_second)) is not None


# ---- 矩阵行 5（ADM-FR-023 / WI-AC-006=ADM-AC-046）：素材动作审计 ----


async def test_admin_asset_rename_and_delete_are_audited(
    admin_client, client, credentials, other_credentials
):
    """asset_rename / asset_delete 留痕：actor/target_type=asset/target_id/
    action/result 正确；summary 含 title/new_title 与 kind（WI 决策 3/4 的
    白名单键），无敏感痕迹（矩阵行 5 / ADM-AC-046）。"""
    admin = await _make_admin(admin_client, credentials)
    await _register(client, other_credentials)
    rename_target = await upload_asset(client)
    delete_target = await upload_asset(client)
    seeded = await client.patch(f"/api/assets/{rename_target}", json={"title": "旧名字"})
    assert seeded.status_code == 200, seeded.text

    renamed = await admin_client.patch(
        f"/api/admin/assets/{rename_target}", json={"title": "新名字"}
    )
    assert renamed.status_code == 200, renamed.text

    deleted = await admin_client.delete(f"/api/admin/assets/{delete_target}")
    assert deleted.status_code in (200, 204), deleted.text
    assert (await _asset_row(delete_target)) is None

    rename_rows = [
        row for row in await _audit_rows(admin["id"], rename_target)
        if row["action"] == "asset_rename"
    ]
    [rename_row] = rename_rows
    assert rename_row["result"] == "success"
    assert rename_row["target_type"] == "asset"
    assert rename_row["actor_id"] == admin["id"]
    assert "旧名字" in rename_row["summary"] and "新名字" in rename_row["summary"]

    delete_rows = [
        row for row in await _audit_rows(admin["id"], delete_target)
        if row["action"] == "asset_delete"
    ]
    [delete_row] = delete_rows
    assert delete_row["result"] == "success"
    assert delete_row["target_type"] == "asset"
    assert "original" in delete_row["summary"]  # summary: kind
    assert delete_row["summary"]  # reason 非空


async def test_admin_batch_delete_audits_only_actually_deleted_items(
    admin_client, client, credentials, other_credentials
):
    """批量只记实际删除项：每个 deleted 项一行 asset_delete，skipped 项不逐条
    留痕（响应已枚举）（矩阵行 5 / ADM-AC-046，WI 决策 5）。"""
    admin = await _make_admin(admin_client, credentials)
    await _register(client, other_credentials)
    deleted_target = await upload_asset(client)
    skipped_session = await open_session(client, title="占用会话")
    skipped_target = skipped_session["current_asset_id"]

    response = await admin_client.post(
        "/api/admin/assets/batch-delete",
        json={"asset_ids": [deleted_target, skipped_target]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] == [deleted_target]
    [skipped] = body["skipped"]
    assert skipped["id"] == skipped_target

    rows = [
        row for row in await _audit_rows(admin["id"])
        if row["action"] == "asset_delete"
    ]
    assert [row["target_id"] for row in rows] == [deleted_target]
    assert all(row["target_type"] == "asset" for row in rows)
    assert all(row["result"] == "success" for row in rows)
    assert skipped_target not in [row["target_id"] for row in rows]
