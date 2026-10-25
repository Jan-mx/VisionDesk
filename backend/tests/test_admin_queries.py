"""WI-20261025-05 任务与操作查询专项测试（Red 阶段）。

对应测试矩阵全部 4 行（docs/work-items/WI-20261025-05-admin-queries.md，
ADM-FR-021/022/024 专项 + ADM-FR-004 回归在既有套件），每条用例 docstring
标注所属矩阵行与验收场景（WI-AC-001~003 = ADM-AC-042~045、048 逐条映射）。
Red 与造数约定（沿用阶段 2~4 先例）：

- 阶段 5 新端点（``/api/admin/tool-runs``、``/api/admin/agent-runs``、
  ``/api/admin/audit-logs`` 全套）尚不存在：请求落入 ``main.py`` 的 ``/api``
  兜底路由得到 404 ``{"detail": "Not Found"}``，每条用例在第一个涉及新端点的
  断言上以具体断言失败呈现 Red；本文件只导入既有模块（routers/admin、
  services/paging、``_sanitize_error`` 等新符号一律不按名导入），天然可收集、
  不存在收集错误；
- 管理员造数沿用阶段 2 ``test_admin_users.py`` 的先例（``_make_admin``）：
  注册 ``test_`` 账号后 SQL 直改 ``role='admin'``，不依赖尚不存在的管理端点；
- run 造数双轨：列表/筛选/分页样本以 SQL/ORM 直插（ToolRun 经 text() 直插，
  先例 test_admin_deletion._seed_tool_run；AgentRun 经 ORM，先例
  test_admin_users._insert_agent_run——AgentRun.session_id 非空 FK，直插前
  经用户端 API 造真实会话）；「同轮次关联追踪」与产物链路走真实 HTTP 流程
  （fake_planner 规划 + confirm + run_tool 收尾，先例 tests/test_agent.py）；
- 开发库存在非 ``test_`` 前缀的真实账号与任务（手工冒烟遗留）：凡涉及列表
  total、排序、筛选的断言，一律以 WI 固化契约的同口径 SQL 为预期基准（同过滤
  条件、同排序键、id 同方向稳定次序），不假设库内只有测试数据；
- 审计断言走 SQLAlchemy 直查/ORM 直插 ``admin_audit_logs``（表自阶段 2 存在）；
  本模块直插的审计行/清理操作行/对象存储键在 autouse 夹具中自清（两者均无
  外键，不随用户级联），绝不触碰非测试数据；
- 陈旧 run 的失联判定前提用 ``services.runs.is_stale`` 纯函数自检（只读判定，
  不触发 ``runs.get``/``expire_stale`` 的落库路径）；Redis 零事件经
  ``events.redis_client`` 订阅被测 run 频道后断言窗口期内无消息（publish 形态
  见 app/events.py:19-24，频道名 ``run:{id}``）。

无法从模型/契约唯一固化之处的诚实变体（实现如偏离需回改断言而非放松语义）：

- WI 决策 3 的 ``turn``（轮次字段如实）：AgentRun 模型无持久化轮次序号列
  （app/models/agent_run.py 全字段为 id/user_id/session_id/revision/goal/
  reply/plan/status/error），不预设编号约定，只断言响应含 ``turn`` 键且为整数；
- 产物列表容器键名 WI 未固化（决策 5 仅固化条目形态 ``{"asset_id": ...,
  "missing": true}``）：以「递归搜索含指定 asset_id 的条目」锚定语义，签名
  URL 键名同法（断言条目中存在含 ``X-Amz-Signature`` 的字符串值）；
- plan 步骤摘要的容器键名未固化：``steps``/``plan`` 任一为列表即认（每步仅
  tool 名与 status 的白名单仍按 WI 决策 3 从严断言）；
- 写方法未注册的语义（WI 矩阵「405/404 语义」）：``main.py:104`` 的 ``/api``
  兜底路由注册了全部写方法，未注册路径按兜底返回 404——断言写方法 ∈
  {404, 405}（未注册语义）且零落库，不预设路由级 405；
- 决策 5「剔除原始 response body」从严断言：两个错误样本分别把长响应体放在
  错误串尾部（多形态样本）与最前（纯响应体样本，截断窗口内必然命中其重复段），
  同时断言「全文不出现」与「首个重复段不出现」——仅截断到 200 而不剔除响应体、
  URL、密钥的实现会在任一样本上失败；「保留首行/剥离 URL、密钥、Authorization
  键值/剔除 JSON 体」的合规实现均可通过。
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import delete, text

from app import events, storage
from app.agent import graph
from app.config import get_settings
from app.db import SessionFactory
from app.main import app
from app.models import AccountDeletionOperation, AdminAuditLog, AgentRun
from app.models.account_deletion_operation import (
    PHASE_OBJECTS,
    AccountDeletionStatus,
)
from app.services import runs
from app.tasks.tools import run_tool
from tests.test_agent import FakePlanner, send, tool_call, tool_calls
from tests.test_assets import make_image, open_session

# ---- 注入样本（仅存在于测试造数中；任何管理响应都不得出现）----

SECRET_PROMPT = "机密提示词SECRET-PROMPT-7f3a9c"
SECRET_KEY = "sk-injected-leaktest-0123456789abcdef"
SECRET_TOKEN = "eyJhbGciOiJIUzUxMiJ9.leaksig-do-not-leak"
SIGNED_URL_SAMPLE = (
    "https://minio.example.com/visiondesk/users/11111111-1111-1111-1111-111111111111"
    "/artifact.png?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Signature=0123456789abcdefdeadbeefcafe&X-Amz-Expires=300"
)
LEAK_BODY_UNIT = "LEAKBODY0123456789abcdef"
LEAK_BODY = LEAK_BODY_UNIT * 12  # 312 字符：截断到 200 也必然残留重复段
LEAK_BODY2_UNIT = "RAWSPL0123456789"
LEAK_BODY2 = LEAK_BODY2_UNIT * 12
# 第二注入样本：原始响应体位于错误串最前（总长 ~278 字符）——仅截断到 200 而
# 不剔除 response body 的实现必然残留 LEAK_BODY2 的重复段，被下方断言捕获
RESPONSE_BODY_ERROR = (
    'response: {"code": "InternalError", "request_id": "req-admin-queries",'
    ' "message": "' + LEAK_BODY2 + '"}'
)
SECRET_GOAL = "机密目标SECRET-GOAL-91bd24"
SECRET_REPLY = "机密回复SECRET-REPLY-44ce81"
INJECTED_ERROR = (
    "UpstreamProviderError: 上游模型调用失败\n"
    f"request: POST {SIGNED_URL_SAMPLE}\n"
    'headers: {"Authorization": "Bearer ' + SECRET_TOKEN + '",'
    ' "X-Api-Key": "' + SECRET_KEY + '"}\n'
    'response: {"code": "InternalError", "message": "' + LEAK_BODY + '"}'
)
# 注入样本的「不得出现在任何管理响应中」清单（全文搜索断言用）
_INJECTED_SECRETS = (
    SECRET_PROMPT,
    SECRET_KEY,
    SECRET_TOKEN,
    SIGNED_URL_SAMPLE,
    LEAK_BODY,
    LEAK_BODY_UNIT * 2,  # 首个重复段：仅截断不剔除响应体的实现会残留
    LEAK_BODY2,
    LEAK_BODY2_UNIT * 2,
    "X-Amz-Signature",
)


# ---- 造数与断言助手（沿用阶段 2~4 先例）----


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


@pytest.fixture
def fake_planner(monkeypatch):
    """与 tests/test_agent.py 相同的假规划器安装器（本地重声明，避免导入名与
    用例参数同名，先例 tests/test_admin_users.py）。"""

    def install(message, *more):
        fake = FakePlanner(message, *more)
        monkeypatch.setattr(graph, "planner", lambda: fake)
        return fake

    return install


_TRACKED_AUDIT_IDS: set[uuid.UUID] = set()
_TRACKED_OPERATION_IDS: set[uuid.UUID] = set()
_TRACKED_OBJECT_KEYS: set[str] = set()


@pytest.fixture(autouse=True)
async def purge_admin_query_artifacts():
    """自清本模块直插的审计行/清理操作行/对象存储键（均无外键，不随用户级联）。

    直插的 tool_runs/agent_runs/assets 随 test_ 用户由 conftest cleanup_users
    级联删除，此处不重复处理。
    """
    yield
    if _TRACKED_AUDIT_IDS or _TRACKED_OPERATION_IDS:
        async with SessionFactory() as session:
            if _TRACKED_AUDIT_IDS:
                await session.execute(
                    delete(AdminAuditLog).where(AdminAuditLog.id.in_(_TRACKED_AUDIT_IDS))
                )
            if _TRACKED_OPERATION_IDS:
                await session.execute(
                    delete(AccountDeletionOperation).where(
                        AccountDeletionOperation.id.in_(_TRACKED_OPERATION_IDS)
                    )
                )
            await session.commit()
    for key in list(_TRACKED_OBJECT_KEYS):
        try:
            await storage.delete(key)
        except Exception:  # noqa: BLE001 — 残留清理尽力而为，不掩盖断言结果
            pass
    _TRACKED_AUDIT_IDS.clear()
    _TRACKED_OPERATION_IDS.clear()
    _TRACKED_OBJECT_KEYS.clear()


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


async def _register_user(client: httpx.AsyncClient, credentials: dict) -> str:
    """注册 test_ 账号（注册即自动登录），返回 user id。"""
    assert (await client.post("/api/auth/register", json=credentials)).status_code == 201
    me = await client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    return me.json()["id"]


async def _register_with_session(client: httpx.AsyncClient, credentials: dict) -> tuple[str, str]:
    """注册并经用户端 API 造一个真实会话（AgentRun 直插的 session FK 前提）。"""
    user_id = await _register_user(client, credentials)
    session = await open_session(client)
    return user_id, session["id"]


async def _scalar(sql: str, **params):
    async with SessionFactory() as session:
        return (await session.execute(text(sql), params)).scalar_one()


async def _ordered_ids(
    table: str,
    order: str = "desc",
    where: str = "TRUE",
    *,
    column: str = "created_at",
    **params,
) -> list[str]:
    """列表断言的契约口径 oracle：主排序 + id 同方向稳定次序（paging.paginate）。

    由 WI 文本转写而非读取实现；开发库的非测试数据经由相同过滤条件纳入预期。
    column 为主排序列（WI 排序白名单内的实际列，与端点同列同方向——经主会话
    授权修正：原实现硬编码 created_at，与 status/action 排序断言自相矛盾）。
    """
    assert table in {"tool_runs", "agent_runs", "admin_audit_logs"}
    assert order in ("asc", "desc")
    assert column in {"created_at", "status", "tool", "action", "result"}
    async with SessionFactory() as session:
        rows = await session.execute(
            text(
                f"SELECT id::text FROM {table} WHERE {where}"
                f" ORDER BY {column} {order}, id {order}"
            ),
            params,
        )
        return rows.scalars().all()


async def _insert_tool_run(
    user_id: str,
    *,
    session_id: str | None = None,
    tool: str = "generate_image",
    status: str = "queued",
    params: dict | None = None,
    result: dict | None = None,
    error: str | None = None,
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    heartbeat_at: datetime | None = None,
) -> str:
    """SQL 直插 ToolRun（全字段可控；先例 test_admin_deletion._seed_tool_run）。"""
    run_id = uuid.uuid4()
    sql = (
        "INSERT INTO tool_runs (id, user_id, session_id, tool, status, progress,"
        " stage, params, result, error, retries, created_at, started_at,"
        " finished_at, heartbeat_at)"
        " VALUES (:id, :u, :s, :tool, :status, 0, '',"
        " CAST(:params AS jsonb), CAST(:result AS jsonb), :error, 0,"
        " :created_at, :started_at, :finished_at, :heartbeat_at)"
    )
    bound = {
        "id": run_id,
        "u": uuid.UUID(user_id),
        "s": uuid.UUID(session_id) if session_id else None,
        "tool": tool,
        "status": status,
        "params": "{}",
        "result": "{}",
        "error": error,
        "created_at": created_at or datetime.now(UTC),
        "started_at": started_at,
        "finished_at": finished_at,
        "heartbeat_at": heartbeat_at,
    }
    if params is not None:
        bound["params"] = json.dumps(params, ensure_ascii=False)
    if result is not None:
        bound["result"] = json.dumps(result, ensure_ascii=False)
    async with SessionFactory() as session:
        await session.execute(text(sql), bound)
        await session.commit()
    return str(run_id)


async def _insert_agent_run(
    user_id: str,
    session_id: str,
    *,
    status: str = "succeeded",
    goal: str = "测试轮次",
    reply: str = "",
    plan: list | None = None,
    error: str | None = None,
    created_at: datetime | None = None,
) -> str:
    """ORM 直插 AgentRun（先例 test_admin_users._insert_agent_run，可带敏感样本）。"""
    turn = AgentRun(
        user_id=uuid.UUID(user_id),
        session_id=uuid.UUID(session_id),
        revision=1,
        goal=goal,
        reply=reply,
        plan=plan if plan is not None else [],
        status=status,
        error=error,
        created_at=created_at or datetime.now(UTC),
    )
    async with SessionFactory() as session:
        session.add(turn)
        await session.commit()
    return str(turn.id)


async def _insert_asset(
    user_id: str,
    *,
    kind: str = "generated",
    source: str = "generate",
    with_object: bool = False,
) -> tuple[str, str]:
    """直插素材行（先例 test_admin_users._insert_hidden_asset），可选落真实对象。"""
    asset_id = uuid.uuid4()
    key = f"test/admin-queries/{uuid.uuid4()}"
    async with SessionFactory() as session:
        await session.execute(
            text(
                "INSERT INTO assets (id, user_id, kind, source, storage_key,"
                " image_format, width, height, size_bytes, has_alpha, library_hidden)"
                " VALUES (:id, :user_id, :kind, :source, :key, 'PNG', 64, 64,"
                " 128, false, false)"
            ),
            {
                "id": asset_id,
                "user_id": uuid.UUID(user_id),
                "kind": kind,
                "source": source,
                "key": key,
            },
        )
        await session.commit()
    if with_object:
        await storage.put(key, make_image((64, 64)), "image/png")
        _TRACKED_OBJECT_KEYS.add(key)
    return str(asset_id), key


async def _insert_audit_row(
    *,
    actor_id: str,
    target_type: str = "user",
    target_id: str | None = None,
    action: str = "user_rename",
    result: str = "success",
    request_id: str | None = "req-admin-queries",
    summary: dict | None = None,
    operation_id: str | None = None,
    created_at: datetime | None = None,
) -> str:
    """ORM 直插审计行（表自阶段 2 存在），登记自清并返回 id。"""
    row_id = uuid.uuid4()
    async with SessionFactory() as session:
        session.add(
            AdminAuditLog(
                id=row_id,
                actor_id=uuid.UUID(actor_id),
                target_type=target_type,
                target_id=uuid.UUID(target_id) if target_id else uuid.uuid4(),
                action=action,
                result=result,
                request_id=request_id,
                summary=summary,
                operation_id=uuid.UUID(operation_id) if operation_id else None,
                created_at=created_at or datetime.now(UTC),
            )
        )
        await session.commit()
    _TRACKED_AUDIT_IDS.add(row_id)
    return str(row_id)


async def _insert_operation(*, status: str = "failed") -> str:
    """直插一条清理操作（无 FK，自清登记；字段先例 app/models/account_deletion_operation.py）。"""
    operation_id = uuid.uuid4()
    async with SessionFactory() as session:
        session.add(
            AccountDeletionOperation(
                id=operation_id,
                target_user_id=uuid.uuid4(),
                actor_id=uuid.uuid4(),
                status=AccountDeletionStatus(status),
                phase=PHASE_OBJECTS,
                progress_done=3,
                progress_total=7,
                attempt_count=2,
                failure_code="objects_list_failed" if status == "failed" else None,
            )
        )
        await session.commit()
    _TRACKED_OPERATION_IDS.add(operation_id)
    return str(operation_id)


async def _run_row(run_id: str) -> dict:
    """ToolRun 行快照（陈旧零变化断言的 before/after 基准）。"""
    async with SessionFactory() as session:
        row = await session.execute(
            text(
                "SELECT status::text AS status, error, stage, progress, retries,"
                " started_at, heartbeat_at, finished_at, created_at"
                " FROM tool_runs WHERE id = :r"
            ),
            {"r": uuid.UUID(run_id)},
        )
        return dict(row.mappings().one())


async def _audit_count(actor_id: str) -> int:
    return await _scalar(
        "SELECT count(*) FROM admin_audit_logs WHERE actor_id = :a", a=uuid.UUID(actor_id)
    )


def _parse_iso(value) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _find_artifact_entries(node, asset_id: str) -> list[dict]:
    """递归搜索响应 JSON 中含指定 asset_id 的产物条目（容器键名 WI 未固化）。"""
    found: list[dict] = []
    if isinstance(node, dict):
        if str(node.get("asset_id", "")) == asset_id:
            found.append(node)
        for value in node.values():
            found.extend(_find_artifact_entries(value, asset_id))
    elif isinstance(node, list):
        for value in node:
            found.extend(_find_artifact_entries(value, asset_id))
    return found


def _find_operation(node, operation_id: str) -> list[dict]:
    """递归搜索嵌套的 DeletionOperationOut（以 id + target_user_id 双键识别，
    与扁平 operation_id 字符串字段区分）。"""
    found: list[dict] = []
    if isinstance(node, dict):
        if str(node.get("id", "")) == operation_id and "target_user_id" in node:
            found.append(node)
        for value in node.values():
            found.extend(_find_operation(value, operation_id))
    elif isinstance(node, list):
        for value in node:
            found.extend(_find_operation(value, operation_id))
    return found


def _steps_of(detail: dict) -> list:
    """plan 步骤摘要列表（容器键名 WI 未固化：steps/plan 任一）。"""
    for key in ("steps", "plan"):
        if isinstance(detail.get(key), list):
            return detail[key]
    return []


def _assert_no_secrets(response: httpx.Response) -> None:
    """注入样本响应全文搜索断言（WI 决策 5 / ADM-AC-045）。"""
    for secret in _INJECTED_SECRETS:
        assert secret not in response.text, secret


def _probes() -> list[tuple[str, str, dict | None]]:
    """全部阶段 5 新端点的探针：静态路径与 /{id} 路径各分支。"""
    missing_id = str(uuid.uuid4())
    return [
        ("GET", "/api/admin/tool-runs", None),
        ("GET", f"/api/admin/tool-runs/{missing_id}", None),
        ("GET", "/api/admin/agent-runs", None),
        ("GET", f"/api/admin/agent-runs/{missing_id}", None),
        ("GET", "/api/admin/audit-logs", None),
        ("GET", f"/api/admin/audit-logs/{missing_id}", None),
    ]


_PROBE_IDS = [
    "tool-runs-list",
    "tool-runs-detail",
    "agent-runs-list",
    "agent-runs-detail",
    "audit-logs-list",
    "audit-logs-detail",
]


# ===========================================================================
# 矩阵行 1（ADM-FR-021 / WI-AC-001 = ADM-AC-042、043）：
# 两类任务独立列表/筛选/分页/详情、同轮次关联追踪、步骤/产物、时间真实性
# ===========================================================================


async def test_tool_run_list_filters_pagination_sort_and_independence(
    admin_client, client, second_client, credentials, other_credentials, third_credentials
):
    """tool-runs 列表：user_id/status/tool/created_from~to 筛选（下含上不含）、
    created_at/status/tool 排序白名单（id 同方向稳定）、分页切片与越界空页、
    username 归属 join；与 AgentRun 相互不混算（ADM-FR-021 / ADM-AC-042）。"""
    await _make_admin(admin_client, credentials)
    a_id, a_session = await _register_with_session(client, other_credentials)
    b_id, _ = await _register_with_session(second_client, third_credentials)

    # A 用户：queued 生成（有任务参数，列表不透传）+ succeeded 生成（有起止）
    queued = await client.post(
        "/api/generations", json={"prompt": "浅木色桌面上的白色马克杯", "ratio": "1:1", "count": 1}
    )
    assert queued.status_code == 202, queued.text
    queued_id = queued.json()["id"]
    gen = await client.post(
        "/api/generations", json={"prompt": "浅木色桌面上的白色马克杯", "ratio": "1:1", "count": 1}
    )
    assert gen.status_code == 202, gen.text
    await run_tool({}, uuid.UUID(gen.json()["id"]))
    # A 用户：直插 failed / canceled（status 筛选与排序样本，tool 各异）
    failed_id = await _insert_tool_run(
        a_id, status="failed", tool="replace_background", error="上游失败"
    )
    await _insert_tool_run(
        a_id, status="canceled", tool="flip_layer", session_id=a_session
    )
    # B 用户对照行（user_id 筛选隔离）
    b_run_id = await _insert_tool_run(b_id, status="succeeded")
    # A 用户 AgentRun 对照行（两类不混算）
    agent_id = await _insert_agent_run(a_id, a_session, status="succeeded")
    # 时间窗口：下界命中、上界恰好不包含（PRD §8.2）
    lo_time = datetime(2020, 3, 1, tzinfo=UTC)
    hi_time = datetime(2020, 6, 1, tzinfo=UTC)
    lo_id = await _insert_tool_run(a_id, status="failed", tool="flip_layer", created_at=lo_time)
    boundary_id = await _insert_tool_run(
        a_id, status="failed", tool="flip_layer", created_at=hi_time
    )

    scope = "user_id = :u"
    params = {"u": uuid.UUID(a_id)}
    common = {"user_id": a_id, "page_size": 100}

    listed = await admin_client.get("/api/admin/tool-runs", params=common)
    assert listed.status_code == 200, listed.text  # Red：新端点 404 在此失败
    body = listed.json()
    expected = await _ordered_ids("tool_runs", "desc", scope, **params)
    assert [item["id"] for item in body["items"]] == expected
    assert body["total"] == len(expected)
    assert body["page"] == 1 and body["page_size"] == 100
    assert all(item["user_id"] == a_id for item in body["items"])
    assert all(item["username"] == other_credentials["username"] for item in body["items"])
    assert b_run_id not in expected  # 跨用户隔离
    assert agent_id not in expected  # 与 AgentRun 不混算（tool-runs 不含轮次）
    assert queued_id in expected  # 列表含陈旧 queued（白名单字段如实）

    by_status = await admin_client.get("/api/admin/tool-runs", params=common | {"status": "failed"})
    assert by_status.status_code == 200, by_status.text
    expected = await _ordered_ids("tool_runs", "desc", f"{scope} AND status = 'failed'", **params)
    assert [item["id"] for item in by_status.json()["items"]] == expected
    assert failed_id in expected and lo_id in expected

    by_tool = await admin_client.get(
        "/api/admin/tool-runs", params=common | {"tool": "generate_image"}
    )
    assert by_tool.status_code == 200, by_tool.text
    expected = await _ordered_ids(
        "tool_runs", "desc", f"{scope} AND tool = 'generate_image'", **params
    )
    assert [item["id"] for item in by_tool.json()["items"]] == expected
    assert queued_id in expected

    window = {"created_from": "2020-03-01T00:00:00+00:00",
              "created_to": "2020-06-01T00:00:00+00:00"}
    by_range = await admin_client.get("/api/admin/tool-runs", params=common | window)
    assert by_range.status_code == 200, by_range.text
    expected = await _ordered_ids(
        "tool_runs", "desc", f"{scope} AND created_at >= :lo AND created_at < :hi",
        **params, lo=lo_time, hi=hi_time,
    )
    assert [item["id"] for item in by_range.json()["items"]] == expected
    assert lo_id in expected
    assert boundary_id not in expected  # 上界不包含

    combined = await admin_client.get(
        "/api/admin/tool-runs",
        params=common | {"status": "failed", "tool": "flip_layer"} | window,
    )
    assert combined.status_code == 200, combined.text
    expected = await _ordered_ids(
        "tool_runs", "desc",
        f"{scope} AND status = 'failed' AND tool = 'flip_layer'"
        " AND created_at >= :lo AND created_at < :hi",
        **params, lo=lo_time, hi=hi_time,
    )
    assert [item["id"] for item in combined.json()["items"]] == expected

    # 排序白名单：status / tool（oracle 与端点同列同方向：status 白名单序）
    by_status_sort = await admin_client.get(
        "/api/admin/tool-runs", params=common | {"sort": "status", "order": "asc"}
    )
    assert by_status_sort.status_code == 200, by_status_sort.text
    expected = await _ordered_ids("tool_runs", "asc", scope, column="status", **params)
    assert [item["id"] for item in by_status_sort.json()["items"]] == expected

    by_tool_sort = await admin_client.get(
        "/api/admin/tool-runs", params=common | {"sort": "tool", "order": "desc"}
    )
    assert by_tool_sort.status_code == 200, by_tool_sort.text
    async with SessionFactory() as session:
        rows = await session.execute(
            text(f"SELECT id::text FROM tool_runs WHERE {scope}"
                 f" ORDER BY tool DESC, id DESC"),
            params,
        )
        assert [item["id"] for item in by_tool_sort.json()["items"]] == rows.scalars().all()

    # created_at 并列按 id 同方向稳定
    tie_time = datetime(2020, 1, 1, tzinfo=UTC)
    tie_ids = [
        await _insert_tool_run(a_id, status="canceled", tool="rotate_layer", created_at=tie_time)
        for _ in range(3)
    ]
    ascending = await admin_client.get(
        "/api/admin/tool-runs", params=common | {"sort": "created_at", "order": "asc"}
    )
    assert ascending.status_code == 200, ascending.text
    ids = [item["id"] for item in ascending.json()["items"]]
    assert ids[:3] == sorted(tie_ids)  # 并列最旧的三行按 id 升序
    descending = await admin_client.get(
        "/api/admin/tool-runs", params=common | {"sort": "created_at", "order": "desc"}
    )
    assert (
        [item["id"] for item in descending.json()["items"]][-3:]
        == list(reversed(sorted(tie_ids)))
    )

    # 分页切片与越界空页（真实 total）
    expected = await _ordered_ids("tool_runs", "desc", scope, **params)
    sliced = await admin_client.get(
        "/api/admin/tool-runs",
        params={"user_id": a_id, "page": 2, "page_size": 2, "sort": "created_at",
                "order": "desc"},
    )
    assert sliced.status_code == 200, sliced.text
    assert [item["id"] for item in sliced.json()["items"]] == expected[2:4]
    assert sliced.json()["total"] == len(expected)
    beyond = await admin_client.get(
        "/api/admin/tool-runs",
        params={"user_id": a_id, "page": 999, "page_size": 2},
    )
    assert beyond.status_code == 200, beyond.text
    assert beyond.json()["items"] == []
    assert beyond.json()["total"] == len(expected)

    # 参数边界：白名单外 sort、非法 order、无时区时间、反向区间一律 422
    for bad in (
        {"sort": "progress"},
        {"sort": "created_at", "order": "sideways"},
        {"created_from": "2026-01-01T00:00:00"},
        {"created_from": "2026-06-01T00:00:00+00:00",
         "created_to": "2026-01-01T00:00:00+00:00"},
    ):
        response = await admin_client.get("/api/admin/tool-runs", params=bad)
        assert response.status_code == 422, f"{bad}: {response.text}"


async def test_agent_run_list_filters_sort_whitelist_and_independence(
    admin_client, client, second_client, credentials, other_credentials, third_credentials
):
    """agent-runs 列表：user_id/status/session_id/created_from~to 筛选、排序白名单
    仅 created_at/status（sort=tool 拒绝）、白名单字段与 turn 如实、goal/reply/
    params/error 与伪造时间键不出现、与 ToolRun 相互不混算（ADM-FR-021/022 /
    ADM-AC-042，WI 决策 3）。"""
    await _make_admin(admin_client, credentials)
    a_id, s1 = await _register_with_session(client, other_credentials)
    s2 = (await open_session(client))["id"]  # 同用户第二会话
    b_id, b_session = await _register_with_session(second_client, third_credentials)

    turn1 = await _insert_agent_run(
        a_id, s1, status="queued", goal=SECRET_GOAL, reply=SECRET_REPLY,
        created_at=datetime(2020, 3, 1, tzinfo=UTC),
    )
    turn2 = await _insert_agent_run(a_id, s2, status="succeeded")
    turn3 = await _insert_agent_run(
        a_id, s1, status="failed", goal=SECRET_GOAL, error=INJECTED_ERROR,
        created_at=datetime(2020, 6, 1, tzinfo=UTC),
    )
    b_turn = await _insert_agent_run(b_id, b_session, status="succeeded")
    tool_run_id = await _insert_tool_run(a_id, status="queued")  # 不混算对照行
    lo_id = await _insert_agent_run(
        a_id, s2, status="canceled", created_at=datetime(2020, 3, 1, tzinfo=UTC)
    )
    boundary_id = await _insert_agent_run(
        a_id, s2, status="canceled", created_at=datetime(2020, 6, 1, tzinfo=UTC)
    )

    scope = "user_id = :u"
    params = {"u": uuid.UUID(a_id)}
    common = {"user_id": a_id, "page_size": 100}

    listed = await admin_client.get("/api/admin/agent-runs", params=common)
    assert listed.status_code == 200, listed.text  # Red：新端点 404 在此失败
    body = listed.json()
    expected = await _ordered_ids("agent_runs", "desc", scope, **params)
    assert [item["id"] for item in body["items"]] == expected
    assert body["total"] == len(expected)
    assert all(item["username"] == other_credentials["username"] for item in body["items"])
    assert b_turn not in expected
    assert tool_run_id not in expected  # 与 ToolRun 不混算

    [item] = [row for row in body["items"] if row["id"] == turn2]
    for key in ("id", "user_id", "username", "session_id", "status", "created_at"):
        assert key in item, key  # WI 决策 3 白名单核心字段
    assert isinstance(item.get("turn"), int)  # 轮次字段如实（诚实变体：不预设编号约定）
    for forbidden in ("goal", "reply", "params", "error",  # 白名单外与原始 JSON
                      "started_at", "finished_at", "heartbeat_at", "duration_ms"):
        assert forbidden not in item, forbidden  # 无起止字段不编造（ADM-AC-043）

    by_status = await admin_client.get(
        "/api/admin/agent-runs", params=common | {"status": "queued"}
    )
    assert by_status.status_code == 200, by_status.text
    expected = await _ordered_ids("agent_runs", "desc", f"{scope} AND status = 'queued'", **params)
    assert [item["id"] for item in by_status.json()["items"]] == expected
    assert turn1 in expected

    by_session = await admin_client.get(
        "/api/admin/agent-runs", params=common | {"session_id": s1}
    )
    assert by_session.status_code == 200, by_session.text
    expected = await _ordered_ids(
        "agent_runs", "desc", f"{scope} AND session_id = :s", **params, s=uuid.UUID(s1)
    )
    assert [item["id"] for item in by_session.json()["items"]] == expected
    assert turn1 in expected and turn3 in expected and turn2 not in expected

    window = {"created_from": "2020-03-01T00:00:00+00:00",
              "created_to": "2020-06-01T00:00:00+00:00"}
    by_range = await admin_client.get("/api/admin/agent-runs", params=common | window)
    assert by_range.status_code == 200, by_range.text
    expected = await _ordered_ids(
        "agent_runs", "desc", f"{scope} AND created_at >= :lo AND created_at < :hi",
        **params, lo=datetime(2020, 3, 1, tzinfo=UTC), hi=datetime(2020, 6, 1, tzinfo=UTC),
    )
    assert [item["id"] for item in by_range.json()["items"]] == expected
    assert lo_id in expected and boundary_id not in expected  # 上界不包含

    # 排序白名单：created_at / status（oracle 同列同方向）；白名单外 sort=tool 422
    by_status_sort = await admin_client.get(
        "/api/admin/agent-runs", params=common | {"sort": "status", "order": "asc"}
    )
    assert by_status_sort.status_code == 200, by_status_sort.text
    expected = await _ordered_ids("agent_runs", "asc", scope, column="status", **params)
    assert [item["id"] for item in by_status_sort.json()["items"]] == expected
    for bad in ({"sort": "tool"}, {"order": "sideways"},
                {"created_from": "2026-01-01T00:00:00"},
                {"created_from": "2026-06-01T00:00:00+00:00",
                 "created_to": "2026-01-01T00:00:00+00:00"}):
        response = await admin_client.get("/api/admin/agent-runs", params=bad)
        assert response.status_code == 422, f"{bad}: {response.text}"

    # 分页切片
    expected = await _ordered_ids("agent_runs", "desc", scope, **params)
    sliced = await admin_client.get(
        "/api/admin/agent-runs",
        params={"user_id": a_id, "page": 2, "page_size": 2},
    )
    assert sliced.status_code == 200, sliced.text
    assert [item["id"] for item in sliced.json()["items"]] == expected[2:4]
    assert sliced.json()["total"] == len(expected)

    # 反向不混算：tool-runs 列表不含任何轮次
    tool_listed = await admin_client.get(
        "/api/admin/tool-runs", params={"user_id": a_id, "page_size": 100}
    )
    assert tool_listed.status_code == 200, tool_listed.text
    assert {turn1, turn2, turn3}.isdisjoint(
        {item["id"] for item in tool_listed.json()["items"]}
    )


async def test_agent_turn_detail_traces_linked_tool_runs_steps_and_artifacts(
    admin_client, client, credentials, other_credentials, fake_planner
):
    """同轮次关联追踪（真实 HTTP 流程）：单步 generate_image 轮次的产物与步骤、
    多步计划确认后的两个工具任务同轮次可追踪——步骤摘要每步仅 tool 名与 status、
    轮次 session_id 与工具任务 session_id 互指、产物 asset_id/kind 与 DB 一致、
    签名 URL 可取回；goal/reply 不透传（矩阵行 1 / ADM-AC-042）。"""
    await _make_admin(admin_client, credentials)
    user_id, session_id = await _register_with_session(client, other_credentials)

    # 单步轮次：自动派发 → plan 步骤带 run_id；测试扮演 worker 收尾（mock provider）
    fake_planner(
        tool_call(
            "generate_image",
            {"prompt": "浅木色桌面上的白色马克杯", "ratio": "1:1", "count": 1},
        )
    )
    turn = await send(client, session_id, "生成一张马克杯")
    assert turn["status"] == "running"
    run_id = turn["steps"][0]["run_id"]
    await run_tool({}, uuid.UUID(run_id))

    # 多步计划轮次：确认后两个工具任务同轮次关联（先例 test_agent 多步确认）
    fake_planner(
        tool_calls(("flip_layer", {"direction": "horizontal"}), ("rotate_layer", {"angle": 15}))
    )
    turn2 = await send(client, session_id, "水平翻转再旋转 15 度")
    confirmed = await client.post(f"/api/sessions/{session_id}/messages/{turn2['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    confirmed_turn = confirmed.json()
    assert confirmed_turn["status"] == "succeeded"
    linked_run_ids = [step["run_id"] for step in confirmed_turn["steps"]]
    assert all(linked_run_ids), linked_run_ids  # 造数前提：两步都已派发并完成

    detail = await admin_client.get(f"/api/admin/agent-runs/{turn['id']}")
    assert detail.status_code == 200, detail.text  # Red：新端点 404 在此失败
    body = detail.json()
    assert body["id"] == turn["id"]
    assert body["session_id"] == session_id
    assert body["user_id"] == user_id
    assert body["status"] == "succeeded"
    # 步骤摘要：每步仅 tool 名与 status（WI 决策 3 白名单，params 不透传）
    steps = _steps_of(body)
    assert [(step["tool"], step["status"]) for step in steps] == [("generate_image", "succeeded")]
    assert set(steps[0]) == {"tool", "status"}, steps[0]
    for forbidden in ("goal", "reply", "params"):
        assert forbidden not in body, forbidden
    for fabricated in ("started_at", "finished_at", "heartbeat_at", "duration_ms"):
        assert fabricated not in body, fabricated  # Agent 无起止字段不编造

    # 产物：与 DB 中该 run 的 result asset_ids 一致，kind 与素材行一致，URL 可取回
    async with SessionFactory() as session:
        row = await session.execute(
            text("SELECT result FROM tool_runs WHERE id = :r"), {"r": uuid.UUID(run_id)}
        )
        produced = [str(a) for a in (row.scalar_one() or {}).get("asset_ids", [])]
    assert produced, "造数前提：generate_image 应有产物"
    for asset_id in produced:
        entries = _find_artifact_entries(body, asset_id)
        assert entries, asset_id  # 产物 ID 出现在轮次详情
        asset_kind = await _scalar(
            "SELECT kind::text FROM assets WHERE id = :a", a=uuid.UUID(asset_id)
        )
        assert entries[0].get("kind") == asset_kind
        url = next(
            (v for v in entries[0].values() if isinstance(v, str) and "X-Amz-Signature" in v),
            None,
        )
        assert url is not None, entries[0]  # 产物存在 → 短期签名 URL
        async with httpx.AsyncClient() as raw:
            fetched = await raw.get(url)
        assert fetched.status_code == 200, fetched.text

    # 多步轮次详情：两步各自 tool 名与 status，无产物
    detail2 = await admin_client.get(f"/api/admin/agent-runs/{turn2['id']}")
    assert detail2.status_code == 200, detail2.text
    steps2 = _steps_of(detail2.json())
    assert [(step["tool"], step["status"]) for step in steps2] == [
        ("flip_layer", "succeeded"),
        ("rotate_layer", "succeeded"),
    ]
    assert all(set(step) == {"tool", "status"} for step in steps2)

    # 同轮次追踪：每个关联工具任务详情的 session_id 指回同一会话，tool 名一致
    for index, linked_run_id in enumerate(linked_run_ids):
        run_detail = await admin_client.get(f"/api/admin/tool-runs/{linked_run_id}")
        assert run_detail.status_code == 200, run_detail.text
        run_body = run_detail.json()
        assert run_body["session_id"] == session_id
        assert run_body["tool"] == confirmed_turn["steps"][index]["tool"]
        assert _steps_of(run_body) == []  # 工具任务无计划步骤（WI 决策 2：无则空）

    # 不混算：轮次 id 与工具任务 id 分属两个资源空间
    agent_listed = await admin_client.get(
        "/api/admin/agent-runs", params={"user_id": user_id, "page_size": 100}
    )
    agent_ids = {item["id"] for item in agent_listed.json()["items"]}
    tool_listed = await admin_client.get(
        "/api/admin/tool-runs", params={"user_id": user_id, "page_size": 100}
    )
    tool_ids = {item["id"] for item in tool_listed.json()["items"]}
    assert {turn["id"], turn2["id"]} <= agent_ids
    assert {run_id, *linked_run_ids} <= tool_ids
    assert agent_ids.isdisjoint(tool_ids)


async def test_tool_run_duration_requires_both_start_and_finish(
    admin_client, client, credentials, other_credentials
):
    """时间字段真实性（ADM-AC-043）：duration_ms 仅当起止均存在才有值（整 5 秒
    → 5000），queued（无起止）与 running（有始无终）如实为 null 不编造；
    started_at/finished_at/heartbeat_at 与 DB 存值一致（WI 决策 2）。"""
    await _make_admin(admin_client, credentials)
    a_id = await _register_user(client, other_credentials)

    started = datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC)
    finished = datetime(2026, 10, 25, 10, 0, 5, tzinfo=UTC)
    heartbeat = datetime(2026, 10, 25, 10, 0, 3, tzinfo=UTC)
    queued_id = await _insert_tool_run(a_id, status="queued")
    running_id = await _insert_tool_run(
        a_id, status="running", started_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    done_id = await _insert_tool_run(
        a_id, status="succeeded",
        started_at=started, finished_at=finished, heartbeat_at=heartbeat,
    )

    listed = await admin_client.get(
        "/api/admin/tool-runs", params={"user_id": a_id, "page_size": 100}
    )
    assert listed.status_code == 200, listed.text  # Red：新端点 404 在此失败
    items = {item["id"]: item for item in listed.json()["items"]}

    assert "duration_ms" in items[queued_id]
    assert items[queued_id]["duration_ms"] is None
    assert items[queued_id]["started_at"] is None
    assert items[queued_id]["finished_at"] is None
    assert items[queued_id]["heartbeat_at"] is None

    assert items[running_id]["duration_ms"] is None  # 有始无终不编造耗时
    assert _parse_iso(items[running_id]["started_at"]) is not None
    assert items[running_id]["finished_at"] is None

    assert items[done_id]["duration_ms"] == 5000
    assert _parse_iso(items[done_id]["started_at"]) == started
    assert _parse_iso(items[done_id]["finished_at"]) == finished
    assert _parse_iso(items[done_id]["heartbeat_at"]) == heartbeat

    # 详情与列表一致（详情同样含上述时间字段与 duration）
    detail = await admin_client.get(f"/api/admin/tool-runs/{done_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["duration_ms"] == 5000
    assert _parse_iso(detail.json()["started_at"]) == started


async def test_tool_run_detail_404_shape(admin_client, credentials):
    """不存在的任务 ID → 统一 404 结构（矩阵行 1 附属：错误码沿用，WI 决策 7）。"""
    await _make_admin(admin_client, credentials)

    response = await admin_client.get(f"/api/admin/tool-runs/{uuid.uuid4()}")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert isinstance(detail, dict), detail  # Red：兜底路由返回字符串 detail，在此失败
    assert detail["code"] == "not_found"


# ===========================================================================
# 矩阵行 2（ADM-FR-022 / WI-AC-002 = ADM-AC-044、045）：
# 陈旧任务查询零副作用、params/goal/reply/原始 JSON 不透传、敏感注入无泄漏、
# 产物缺失标记与签名 URL
# ===========================================================================


async def test_stale_run_queries_leave_db_state_events_and_audit_untouched(
    admin_client, client, credentials, other_credentials
):
    """陈旧 queued/running 经管理列表+详情查询后：DB 状态/heartbeat/终态字段
    零变化、Redis run 频道零事件、读取零审计行（矩阵行 2 / ADM-AC-044，
    WI 决策 1/6——绝不经过 runs.get 的失联过期路径）。"""
    admin = await _make_admin(admin_client, credentials)
    a_id = await _register_user(client, other_credentials)

    stale_seconds = get_settings().run_stale_seconds
    old = datetime.now(UTC) - timedelta(seconds=stale_seconds * 2)
    queued_id = await _insert_tool_run(a_id, status="queued", created_at=old)
    running_id = await _insert_tool_run(
        a_id, status="running", created_at=old, started_at=old + timedelta(seconds=10)
    )
    # 造数前提自检：两行按产品判定确实符合失联阈值（is_stale 纯函数，无写入）
    async with SessionFactory() as session:
        for run_id in (queued_id, running_id):
            run = await runs.load(session, uuid.UUID(run_id))
            assert runs.is_stale(run, stale_seconds), run_id

    snapshots = {run_id: await _run_row(run_id) for run_id in (queued_id, running_id)}
    audit_before = await _audit_count(admin["id"])

    pubsub = events.redis_client().pubsub()
    try:
        for run_id in (queued_id, running_id):
            await pubsub.subscribe(f"run:{run_id}")  # 频道名先例 app/events.py:19-20
        listed = await admin_client.get(
            "/api/admin/tool-runs", params={"user_id": a_id, "page_size": 100}
        )
        assert listed.status_code == 200, listed.text  # Red：新端点 404 在此失败
        statuses = {item["id"]: item["status"] for item in listed.json()["items"]}
        assert statuses[queued_id] == "queued"  # 如实显示陈旧状态，不伪装失败
        assert statuses[running_id] == "running"
        for run_id in (queued_id, running_id):
            detail = await admin_client.get(f"/api/admin/tool-runs/{run_id}")
            assert detail.status_code == 200, detail.text
            assert detail.json()["status"] in ("queued", "running")
        for run_id in (queued_id, running_id):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.5)
            assert message is None, f"管理查询发布了 Redis 事件: {run_id} {message}"
    finally:
        await pubsub.aclose()

    # DB 零变化（状态/heartbeat/终态/错误/进度逐字段比对）
    for run_id in (queued_id, running_id):
        assert await _run_row(run_id) == snapshots[run_id], run_id
    # 读取零审计（WI 决策 6）
    assert await _audit_count(admin["id"]) == audit_before


async def test_params_goal_reply_and_raw_json_never_reach_responses(
    admin_client, client, credentials, other_credentials
):
    """params（含提示词与密钥形态值）、result 原始 JSON、AgentRun 的 goal/reply/
    plan 内 params 一律不出现在列表或详情响应（矩阵行 2 / ADM-AC-045，
    WI 决策 2/3 全文搜索断言）。"""
    await _make_admin(admin_client, credentials)
    a_id, a_session = await _register_with_session(client, other_credentials)

    tool_run_id = await _insert_tool_run(
        a_id,
        status="succeeded",
        params={"prompt": SECRET_PROMPT, "api_key": SECRET_KEY},
        result={"raw_response": LEAK_BODY, "asset_ids": []},
    )
    agent_run_id = await _insert_agent_run(
        a_id,
        a_session,
        status="failed",
        goal=SECRET_GOAL,
        reply=SECRET_REPLY,
        plan=[{"id": "s1", "tool": "generate_image", "status": "failed",
               "params": {"prompt": SECRET_PROMPT, "api_key": SECRET_KEY}}],
    )

    for resource, run_id in (("tool-runs", tool_run_id), ("agent-runs", agent_run_id)):
        listed = await admin_client.get(
            f"/api/admin/{resource}", params={"user_id": a_id, "page_size": 100}
        )
        assert listed.status_code == 200, listed.text  # Red：新端点 404 在此失败
        _assert_no_secrets(listed)
        detail = await admin_client.get(f"/api/admin/{resource}/{run_id}")
        assert detail.status_code == 200, detail.text
        _assert_no_secrets(detail)

    # 白名单键级断言：params/result 只进 DB，不进响应
    tool_detail = (
        await admin_client.get(f"/api/admin/tool-runs/{tool_run_id}")
    ).json()
    assert "params" not in tool_detail
    assert "result" not in tool_detail
    tool_item = next(
        item
        for item in (
            await admin_client.get(
                "/api/admin/tool-runs", params={"user_id": a_id, "page_size": 100}
            )
        ).json()["items"]
        if item["id"] == tool_run_id
    )
    assert "params" not in tool_item and "result" not in tool_item
    agent_detail = (
        await admin_client.get(f"/api/admin/agent-runs/{agent_run_id}")
    ).json()
    for forbidden in ("goal", "reply", "params"):
        assert forbidden not in agent_detail, forbidden


async def test_injected_error_secrets_are_sanitized_everywhere(
    admin_client, client, credentials, other_credentials
):
    """错误字段注入密钥形态/签名 URL/Authorization 头形态/长原始响应后：列表与
    详情响应全文无任何注入样本；错误摘要 ≤200 字符且无换行（矩阵行 2 /
    ADM-AC-045，WI 决策 5——剔除而非仅截断）。"""
    await _make_admin(admin_client, credentials)
    a_id, a_session = await _register_with_session(client, other_credentials)

    tool_run_id = await _insert_tool_run(a_id, status="failed", error=INJECTED_ERROR)
    body_first_id = await _insert_tool_run(a_id, status="failed", error=RESPONSE_BODY_ERROR)
    agent_run_id = await _insert_agent_run(
        a_id, a_session, status="failed", goal=SECRET_GOAL, error=INJECTED_ERROR
    )

    for resource, run_id in (
        ("tool-runs", tool_run_id),
        ("tool-runs", body_first_id),
        ("agent-runs", agent_run_id),
    ):
        listed = await admin_client.get(
            f"/api/admin/{resource}", params={"user_id": a_id, "page_size": 100}
        )
        assert listed.status_code == 200, listed.text  # Red：新端点 404 在此失败
        _assert_no_secrets(listed)
        detail = await admin_client.get(f"/api/admin/{resource}/{run_id}")
        assert detail.status_code == 200, detail.text
        _assert_no_secrets(detail)
        # 错误摘要形态：类型名 + 截断消息，≤200 字符、去除换行（WI 决策 5）
        summary = detail.json().get("error")
        assert isinstance(summary, str) and summary, detail.text  # 摘要存在且非空
        assert len(summary) <= 200, summary
        assert "\n" not in summary, summary

    # 工具任务列表项白名单不含 error（错误摘要仅详情返回，WI 决策 2）
    tool_item = next(
        item
        for item in (
            await admin_client.get(
                "/api/admin/tool-runs", params={"user_id": a_id, "page_size": 100}
            )
        ).json()["items"]
        if item["id"] == tool_run_id
    )
    assert "error" not in tool_item


async def test_artifact_missing_marker_and_signed_url_fetchable(
    admin_client, client, credentials, other_credentials
):
    """产物引用：存在素材行 → 条目含 ID/kind 与短期签名 URL 且可取回对象；
    素材行已删 → {"asset_id": ..., "missing": true} 标记、无签名 URL、详情不失败
    （矩阵行 2 / ADM-AC-043、045，WI 决策 5）。"""
    await _make_admin(admin_client, credentials)
    a_id = await _register_user(client, other_credentials)

    existing_id, key = await _insert_asset(a_id, with_object=True)
    missing_id = str(uuid.uuid4())  # 模拟素材行已删：result 仍引用但行不存在
    run_id = await _insert_tool_run(
        a_id, status="succeeded", result={"asset_ids": [existing_id, missing_id]}
    )

    detail = await admin_client.get(f"/api/admin/tool-runs/{run_id}")
    assert detail.status_code == 200, detail.text  # 缺失标记不使整个详情失败

    existing_entries = _find_artifact_entries(detail.json(), existing_id)
    assert existing_entries, existing_id
    entry = existing_entries[0]
    assert entry.get("kind") == "generated"  # WI 决策 2：仅 ID 与 kind
    url = next(
        (v for v in entry.values() if isinstance(v, str) and "X-Amz-Signature" in v), None
    )
    assert url is not None, entry  # 产物存在 → 短期签名 URL
    async with httpx.AsyncClient() as raw:
        fetched = await raw.get(url)
    assert fetched.status_code == 200, fetched.text  # 签名 URL 可取回对象
    assert fetched.content == await storage.get(key)

    missing_entries = _find_artifact_entries(detail.json(), missing_id)
    assert missing_entries, missing_id
    missing_entry = missing_entries[0]
    assert missing_entry.get("missing") is True  # 已清理标记（决策 5 字面形态）
    assert not any(
        isinstance(v, str) and "X-Amz-Signature" in v for v in missing_entry.values()
    ), missing_entry  # 缺失产物不给签名 URL


# ===========================================================================
# 矩阵行 3（ADM-FR-024 / WI-AC-003 = ADM-AC-048）：
# audit-logs 筛选/分页/详情（嵌套清理操作）、读取零审计、无写端点
# ===========================================================================


async def test_audit_log_list_filters_sort_pagination_and_default_untruncated(
    admin_client, client, credentials, other_credentials
):
    """audit-logs 列表：actor_id/action/result/target_id/created_from~to 筛选
    （下含上不含）、排序白名单 created_at/action/result、分页切片、无筛选默认
    不截断（total 为全表真实总数）、白名单外 sort 422（矩阵行 3 / ADM-AC-048）。"""
    admin = await _make_admin(admin_client, credentials)
    # 经既有写端点制造真实审计行（阶段 2 已交付）：创建 + 改名
    created = await admin_client.post(
        "/api/admin/users",
        json={"username": f"test_auditq_{uuid.uuid4().hex[:8]}", "password": "secret123"},
    )
    assert created.status_code == 201, created.text
    target_id = created.json()["id"]
    renamed = await admin_client.patch(
        f"/api/admin/users/{target_id}",
        json={"username": f"test_auditq_r_{uuid.uuid4().hex[:6]}"},
    )
    assert renamed.status_code == 200, renamed.text
    # 直插行：不同 action/result/时间/actor（含已删 actor 对照）
    lo_time = datetime(2020, 3, 1, tzinfo=UTC)
    hi_time = datetime(2020, 6, 1, tzinfo=UTC)
    failed_row = await _insert_audit_row(
        actor_id=admin["id"], target_type="asset", action="asset_delete",
        result="failed", created_at=lo_time,
        summary={"kind": "original", "reason": "对象清理失败"},
    )
    boundary_row = await _insert_audit_row(
        actor_id=admin["id"], target_type="asset", action="asset_delete",
        result="failed", created_at=hi_time,
    )
    deleted_actor_row = await _insert_audit_row(
        actor_id=str(uuid.uuid4()), action="user_delete", result="success", created_at=lo_time
    )

    scope = "actor_id = :a"
    params = {"a": uuid.UUID(admin["id"])}
    common = {"actor_id": admin["id"], "page_size": 100}

    listed = await admin_client.get("/api/admin/audit-logs", params=common)
    assert listed.status_code == 200, listed.text  # Red：新端点 404 在此失败
    body = listed.json()
    expected = await _ordered_ids("admin_audit_logs", "desc", scope, **params)
    assert [item["id"] for item in body["items"]] == expected
    assert body["total"] == len(expected)
    assert all(item["actor_id"] == admin["id"] for item in body["items"])
    assert deleted_actor_row not in expected  # actor 筛选隔离

    # 无筛选默认不截断：total 为全表真实总数
    unfiltered = await admin_client.get("/api/admin/audit-logs", params={"page_size": 1})
    assert unfiltered.status_code == 200, unfiltered.text
    assert unfiltered.json()["total"] == await _scalar("SELECT count(*) FROM admin_audit_logs")

    # action / result / target_id 筛选
    by_action = await admin_client.get(
        "/api/admin/audit-logs", params=common | {"action": "asset_delete"}
    )
    assert by_action.status_code == 200, by_action.text
    expected = await _ordered_ids(
        "admin_audit_logs", "desc", f"{scope} AND action = 'asset_delete'", **params
    )
    assert [item["id"] for item in by_action.json()["items"]] == expected
    assert failed_row in expected and boundary_row in expected

    by_result = await admin_client.get(
        "/api/admin/audit-logs", params=common | {"result": "failed"}
    )
    assert by_result.status_code == 200, by_result.text
    expected = await _ordered_ids(
        "admin_audit_logs", "desc", f"{scope} AND result = 'failed'", **params
    )
    assert [item["id"] for item in by_result.json()["items"]] == expected

    by_target = await admin_client.get(
        "/api/admin/audit-logs", params=common | {"target_id": target_id}
    )
    assert by_target.status_code == 200, by_target.text
    expected = await _ordered_ids(
        "admin_audit_logs", "desc", f"{scope} AND target_id = :t",
        **params, t=uuid.UUID(target_id),
    )
    assert [item["id"] for item in by_target.json()["items"]] == expected
    assert len(expected) == 2  # 创建 + 改名两行同目标

    # 时间窗口：下含上不含（跨 actor 验证——含已删 actor 行；经主会话授权修正：
    # 原请求误带 actor 过滤，与下方成员断言及 :1229 的隔离断言自相矛盾）
    window = {"created_from": "2020-03-01T00:00:00+00:00",
              "created_to": "2020-06-01T00:00:00+00:00"}
    by_range = await admin_client.get(
        "/api/admin/audit-logs", params=window | {"page_size": 100}
    )
    assert by_range.status_code == 200, by_range.text
    expected = await _ordered_ids(
        "admin_audit_logs", "desc",
        "created_at >= :lo AND created_at < :hi",
        lo=lo_time, hi=hi_time,
    )
    assert [item["id"] for item in by_range.json()["items"]] == expected
    assert failed_row in expected and deleted_actor_row in expected
    assert boundary_row not in expected
    # 已删 actor 行在列表中照常返回，actor_username 为 null（WI 决策 4 join 语义）
    assert all(
        item["actor_username"] is None
        for item in by_range.json()["items"]
        if item["id"] == deleted_actor_row
    )

    # 排序白名单：action / result（oracle 同列同序）；分页切片
    by_action_sort = await admin_client.get(
        "/api/admin/audit-logs", params=common | {"sort": "action", "order": "asc"}
    )
    assert by_action_sort.status_code == 200, by_action_sort.text
    expected = await _ordered_ids(
        "admin_audit_logs", "asc", scope, column="action", **params
    )
    assert [item["id"] for item in by_action_sort.json()["items"]] == expected
    expected = await _ordered_ids("admin_audit_logs", "desc", scope, **params)
    sliced = await admin_client.get(
        "/api/admin/audit-logs",
        params={"actor_id": admin["id"], "page": 2, "page_size": 2},
    )
    assert sliced.status_code == 200, sliced.text
    assert [item["id"] for item in sliced.json()["items"]] == expected[2:4]
    assert sliced.json()["total"] == len(expected)

    # 参数边界：白名单外 sort（username 属用户资源）、非法 order、反向区间 422
    for bad in ({"sort": "username"}, {"order": "sideways"},
                {"created_from": "2026-06-01T00:00:00+00:00",
                 "created_to": "2026-01-01T00:00:00+00:00"}):
        response = await admin_client.get("/api/admin/audit-logs", params=bad)
        assert response.status_code == 422, f"{bad}: {response.text}"


async def test_audit_log_detail_fields_nested_operation_and_deleted_actor(
    admin_client, credentials
):
    """audit-logs 详情：AdminAuditLogOut 全字段（actor_username join、summary
    原样、operation_id）；operation_id 非空时嵌套返回 DeletionOperationOut（读
    操作详情不受清理开关限制、不含 scope_digest 原始 JSON）；actor 已删则
    username 为 null；不存在 ID 统一 404（矩阵行 3 / ADM-AC-048，WI 决策 4）。"""
    admin = await _make_admin(admin_client, credentials)

    operation_id = await _insert_operation(status="failed")
    row_with_op = await _insert_audit_row(
        actor_id=admin["id"], target_type="user", action="user_delete",
        result="success", request_id="req-admin-queries-op",
        operation_id=operation_id,
    )
    plain_row = await _insert_audit_row(
        actor_id=admin["id"], target_type="asset", action="asset_rename",
        result="success", request_id="req-admin-queries-plain",
        summary={"title": "旧名字", "new_title": "新名字", "kind": "original"},
    )
    deleted_actor_row = await _insert_audit_row(actor_id=str(uuid.uuid4()))

    detail = await admin_client.get(f"/api/admin/audit-logs/{row_with_op}")
    assert detail.status_code == 200, detail.text  # Red：新端点 404 在此失败
    body = detail.json()
    assert body["id"] == row_with_op
    assert body["actor_id"] == admin["id"]
    assert body["actor_username"] == admin["username"]  # actor join
    assert body["target_type"] == "user"
    assert body["action"] == "user_delete"
    assert body["result"] == "success"
    assert body["request_id"] == "req-admin-queries-op"
    assert body["operation_id"] == operation_id
    assert "created_at" in body
    # 嵌套清理操作：DeletionOperationOut 字段与 DB 一致，无 scope_digest
    nested = _find_operation(body, operation_id)
    assert len(nested) == 1, body
    assert nested[0]["status"] == "failed"
    assert nested[0]["phase"] == PHASE_OBJECTS
    assert nested[0]["attempt_count"] == 2
    assert nested[0]["progress_done"] == 3
    assert nested[0]["progress_total"] == 7
    assert nested[0]["failure_code"] == "objects_list_failed"
    assert "scope_digest" not in nested[0]  # 对象 key 清单/原始 JSON 不外传

    plain = await admin_client.get(f"/api/admin/audit-logs/{plain_row}")
    assert plain.status_code == 200, plain.text
    plain_body = plain.json()
    assert plain_body["summary"] == {"title": "旧名字", "new_title": "新名字",
                                     "kind": "original"}  # 白名单摘要原样
    assert plain_body["operation_id"] is None
    assert _find_operation(plain_body, operation_id) == []  # 无 operation 不嵌套

    deleted_actor = await admin_client.get(f"/api/admin/audit-logs/{deleted_actor_row}")
    assert deleted_actor.status_code == 200, deleted_actor.text
    assert deleted_actor.json()["actor_username"] is None  # actor 已删 → null

    missing = await admin_client.get(f"/api/admin/audit-logs/{uuid.uuid4()}")
    assert missing.status_code == 404
    missing_detail = missing.json()["detail"]
    assert isinstance(missing_detail, dict), missing_detail
    assert missing_detail["code"] == "not_found"


async def test_audit_log_reads_unaudited_and_write_methods_unregistered(
    admin_client, credentials
):
    """读取零审计（列表/筛选/详情后审计行数不变）；写方法未注册——POST/PATCH/
    PUT/DELETE 对集合与条目路径均为未注册语义（404/405）且零落库（矩阵行 3 /
    ADM-AC-048，WI 决策 4「不提供任何写端点」；/api 兜底路由注册了全部写方法
    （main.py:104），未注册路径按兜底 404，路由存在时为 405）。"""
    admin = await _make_admin(admin_client, credentials)
    row_id = await _insert_audit_row(actor_id=admin["id"])
    before = await _audit_count(admin["id"])

    listed = await admin_client.get("/api/admin/audit-logs", params={"page_size": 25})
    assert listed.status_code == 200, listed.text  # Red：新端点 404 在此失败
    filtered = await admin_client.get(
        "/api/admin/audit-logs", params={"action": "user_rename", "page_size": 25}
    )
    assert filtered.status_code == 200, filtered.text
    detail = await admin_client.get(f"/api/admin/audit-logs/{row_id}")
    assert detail.status_code == 200, detail.text
    assert await _audit_count(admin["id"]) == before  # 读取零审计

    # 未注册写方法（集合与条目各分支）：零写入的未注册语义
    for method, path in (
        ("POST", "/api/admin/audit-logs"),
        ("PATCH", "/api/admin/audit-logs"),
        ("PUT", "/api/admin/audit-logs"),
        ("DELETE", "/api/admin/audit-logs"),
        ("PATCH", f"/api/admin/audit-logs/{row_id}"),
        ("PUT", f"/api/admin/audit-logs/{row_id}"),
        ("DELETE", f"/api/admin/audit-logs/{row_id}"),
    ):
        response = await admin_client.request(method, path, json={"result": "success"})
        assert response.status_code in (404, 405), f"{method} {path}: {response.text}"
    assert await _audit_count(admin["id"]) == before  # 写尝试零落库
    row = await _scalar(
        "SELECT result FROM admin_audit_logs WHERE id = :r", r=uuid.UUID(row_id)
    )
    assert row in ("success", "rejected", "failed")  # 原值未被改写


# ===========================================================================
# 端点级 401/403（矩阵行 1~3 附属，与阶段 2~4 同款闭合 ADM-AC-003）
# ===========================================================================


@pytest.mark.parametrize(("method", "path", "body"), _probes(), ids=_PROBE_IDS)
async def test_admin_query_endpoints_require_authentication(method, path, body):
    """全部阶段 5 新端点未登录一律 401（ADM-AC-003 同口径）。"""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as anonymous:
        response = await anonymous.request(method, path, json=body)
    assert response.status_code == 401, response.text


@pytest.mark.parametrize(("method", "path", "body"), _probes(), ids=_PROBE_IDS)
async def test_admin_query_endpoints_reject_non_admin_users(
    client, credentials, method, path, body
):
    """全部阶段 5 新端点对普通用户一律 403（ADM-AC-003 同口径）。"""
    await client.post("/api/auth/register", json=credentials)

    response = await client.request(method, path, json=body)

    assert response.status_code == 403, response.text
