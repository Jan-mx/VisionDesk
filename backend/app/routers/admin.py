"""管理端用户维护端点（WI-20261025-02 实现要求 4~8；阶段 3 账号删除 WI-20261025-03；
阶段 4 管理端素材 WI-20261025-04）。

全部端点先经 RequireAdmin 鉴权再访问目标；静态路径注册在 /{id} 之前；
错误结构沿用 detail.code/message，404 不区分不存在原因。
统计聚合用相关子查询（不产生 JOIN，total 不被抬高）；写动作与审计同事务，
预期拒绝在业务回滚后另起事务补记 rejected（services/audit.py）。

账号删除（WI-20261025-03 决策 3）：开关关闭时 deletion-preview 与 DELETE 均
503 deletion_unavailable、零写入零审计；确认校验序为 签名/目标(422) → 确认名
与凭证载荷(409) → 操作者(422) → 过期(409) → 目标用户锁内 重读用户名/重算范围
摘要(409) / 活动任务(409) / 所有者保护(409) → 已有未终结操作幂等返回(202)。

管理端素材（WI-20261025-04 决策 1~7）：列表/详情跨用户全量（含隐藏与内部
派生），引用扫描与用户端同一口径（services/assets.py::asset_references）；
单删/批删逐项直接调用 delete_for_user（不改其签名与语义，路由层映射两态
响应与 409 原因），批删请求级全量核验（缺失整批 404 零写入）后逐项独立
事务；动作留痕 asset_rename/asset_delete（批量只记实际删除项）；preview_url
短期签名、不入审计。

任务与操作查询（WI-20261025-05 决策 1~7）：tool-runs / agent-runs / audit-logs
三组只读端点全部直接 select / session.get（只读红线——绝不经过 runs.get、
expire_stale 等会落库发事件的路径），读取零审计、不注册任何写方法（未注册
语义落 /api 兜底 404）；白名单 Out 模型（params/goal/reply/原始 JSON 一律不
透传），错误摘要经 _sanitize_error（剔除 URL 查询串/密钥形态键值/签名 URL/
原始 response body，只留异常类型名 + ≤200 字符消息）；产物缺失 missing 标记
不使详情失败、存在则给短期签名 URL；audit-logs 详情按 operation_id 嵌套
DeletionOperationOut（读操作详情不受清理开关限制）。
"""

import logging
import re
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import ColumnElement, Select, func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app import storage
from app.config import get_settings
from app.db import SessionDep
from app.deps import RequireAdmin
from app.models import (
    AdminAuditLog,
    AgentRun,
    Asset,
    EditSession,
    ToolRun,
    User,
)
from app.models.account_deletion_operation import (
    PHASE_FROZEN,
    AccountDeletionOperation,
    AccountDeletionStatus,
)
from app.models.asset import UNMANAGEABLE_KINDS
from app.models.user import UserStatus
from app.schemas.admin import (
    AccountDeletionPreview,
    AdminAgentRunDetailOut,
    AdminAgentRunListParams,
    AdminAgentRunOut,
    AdminAssetDeleteOut,
    AdminAssetDetailOut,
    AdminAssetListParams,
    AdminAssetOut,
    AdminAssetPatchIn,
    AdminAuditLogDetailOut,
    AdminAuditLogListParams,
    AdminAuditLogOut,
    AdminToolRunDetailOut,
    AdminToolRunListParams,
    AdminToolRunOut,
    AdminUserOut,
    AdminUserPatch,
    DeletionConfirmInput,
    DeletionCounts,
    DeletionOperationOut,
    ResetPasswordInput,
    UserListParams,
)
from app.schemas.auth import Credentials
from app.schemas.deletion import (
    BatchDeleteIn,
    BatchDeleteOut,
    BatchSkippedItem,
    DeletionBlocker,
    DeletionErrorDetail,
    ReferenceOut,
)
from app.security import (
    PreviewTokenError,
    PreviewTokenExpired,
    hash_password,
    issue_account_deletion_token,
    read_account_deletion_token,
)
from app.services import account_deletion, audit, guards, paging, runs
from app.services import assets as asset_service
from app.services.paging import Page
from app.services.runs import ActiveTasks
from app.services.sessions import ReferenceStateInvalid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# 排序白名单（PRD §8.2：sort 只能来自资源白名单，不接受任意 SQL/JSON 路径）
_SORT_COLUMNS = {
    "created_at": User.created_at,
    "username": User.username,
    "status": User.status,
}


def _conflict(code: str, message: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail={"code": code, "message": message})


def _not_found() -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND, detail={"code": "not_found", "message": "用户不存在"}
    )


def _unavailable() -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": "deletion_unavailable", "message": "账号清理功能未开启"},
    )


def _invalid_token(message: str) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"code": "invalid_deletion_token", "message": message},
    )


def _require_deletion_enabled() -> None:
    """清理开关（FR-027）：关闭即 503，先于一切读写与审计。"""
    if not get_settings().admin_account_deletion_enabled:
        raise _unavailable()


def _q_criterion(needle: str) -> ColumnElement[bool] | None:
    """q 命中规则（PRD §8.2）：用户名子串包含（通配符转义防注入）或完整 UUID。"""
    if not needle:
        return None
    try:
        target_id = uuid.UUID(needle)
    except ValueError:
        pattern = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return User.username.ilike(f"%{pattern}%", escape="\\")
    return User.id == target_id


def _stats_columns() -> tuple[Any, ...]:
    """统计聚合相关子查询（WI 实现要求 7，PRD §7）。

    asset_count / asset_record_bytes 覆盖全部 kind（含 library_hidden 与内部派生）；
    session/tool_run/agent_run 分别计数，两类任务不得混算。
    """
    return (
        select(func.count())
        .select_from(Asset)
        .where(Asset.user_id == User.id)
        .scalar_subquery()
        .label("asset_count"),
        select(func.coalesce(func.sum(Asset.size_bytes), 0))
        .select_from(Asset)
        .where(Asset.user_id == User.id)
        .scalar_subquery()
        .label("asset_record_bytes"),
        select(func.count())
        .select_from(EditSession)
        .where(EditSession.user_id == User.id)
        .scalar_subquery()
        .label("session_count"),
        select(func.count())
        .select_from(ToolRun)
        .where(ToolRun.user_id == User.id)
        .scalar_subquery()
        .label("tool_run_count"),
        select(func.count())
        .select_from(AgentRun)
        .where(AgentRun.user_id == User.id)
        .scalar_subquery()
        .label("agent_run_count"),
    )


def _user_with_stats() -> Select:
    return select(User, *_stats_columns())


def _to_out(row: Any) -> AdminUserOut:
    user = row.User
    return AdminUserOut(
        id=user.id,
        username=user.username,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        asset_count=row.asset_count,
        asset_record_bytes=row.asset_record_bytes,
        session_count=row.session_count,
        tool_run_count=row.tool_run_count,
        agent_run_count=row.agent_run_count,
    )


def _fresh_out(user: User) -> AdminUserOut:
    """新建账号视图：尚无业务数据统计全零；last_login_at 如实为空（ADM-AC-012）。"""
    return AdminUserOut(
        id=user.id,
        username=user.username,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        asset_count=0,
        asset_record_bytes=0,
        session_count=0,
        tool_run_count=0,
        agent_run_count=0,
    )


@router.get("/users", response_model=Page[AdminUserOut])
async def list_users(
    params: Annotated[UserListParams, Query()],
    _admin: RequireAdmin,
    session: SessionDep,
) -> Page[AdminUserOut]:
    """用户列表（ADM-FR-004/005）：服务端分页 + 白名单排序 + q/状态/注册时间筛选。

    普通列表读取不产生审计行（PRD FR-024）。
    """
    stmt = _user_with_stats()
    if params.q is not None:
        criterion = _q_criterion(params.q.strip())
        if criterion is not None:
            stmt = stmt.where(criterion)
    if params.status is not None:
        stmt = stmt.where(User.status == params.status)
    if params.created_from is not None:
        stmt = stmt.where(User.created_at >= params.created_from)
    if params.created_to is not None:
        # 区间上界不包含（PRD §8.2：下界包含、上界不包含）
        stmt = stmt.where(User.created_at < params.created_to)
    rows, total = await paging.paginate(session, stmt, params, _SORT_COLUMNS)
    return Page(
        items=[_to_out(row) for row in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.post("/users", status_code=status.HTTP_201_CREATED, response_model=AdminUserOut)
async def create_user(
    payload: Credentials,
    request: Request,
    admin: RequireAdmin,
    session: SessionDep,
) -> AdminUserOut:
    """创建普通账号（ADM-FR-006）：不签发会话、不设置任何 Cookie，不返回凭据。"""
    request_id = audit.request_id_from(request)
    # ORM 对象属性在 rollback 后会过期，回滚路径需要用的值先取成本地量
    actor_id = admin.id
    existing = await session.scalar(select(User).where(User.username == payload.username))
    if existing is not None:
        # 预检命中：业务零写入。被拒的创建目标是「尚未存在的账号」，不把动作
        # 归到已占用该用户名的现有用户头上，target_id 用随机 UUID（实现定义）。
        await audit.record_rejected(
            actor_id=actor_id,
            target_type=audit.TARGET_USER,
            target_id=uuid.uuid4(),
            action=audit.USER_CREATE,
            request_id=request_id,
            summary={"username": payload.username},
        )
        raise _conflict("username_taken", "该用户名已被占用")

    user = User(
        id=uuid.uuid4(),
        username=payload.username,
        password_hash=hash_password(payload.password),
    )
    audit.record(
        session,
        actor_id=actor_id,
        target_type=audit.TARGET_USER,
        target_id=user.id,
        action=audit.USER_CREATE,
        request_id=request_id,
        summary={"username": payload.username},
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:  # 并发兜底：预检与提交之间被抢注
        await session.rollback()
        await audit.record_rejected(
            actor_id=actor_id,
            target_type=audit.TARGET_USER,
            target_id=uuid.uuid4(),
            action=audit.USER_CREATE,
            request_id=request_id,
            summary={"username": payload.username},
        )
        raise _conflict("username_taken", "该用户名已被占用") from None
    return _fresh_out(user)


@router.get("/users/{user_id}", response_model=AdminUserOut)
async def get_user(
    user_id: uuid.UUID,
    _admin: RequireAdmin,
    session: SessionDep,
) -> AdminUserOut:
    """用户详情（ADM-FR-005）：含完整统计，列表与详情一致（ADM-AC-011）。"""
    row = (await session.execute(_user_with_stats().where(User.id == user_id))).first()
    if row is None:
        raise _not_found()
    return _to_out(row)


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def patch_user(
    user_id: uuid.UUID,
    payload: AdminUserPatch,
    request: Request,
    admin: RequireAdmin,
    session: SessionDep,
) -> AdminUserOut:
    """改名与启停（ADM-FR-007/009/010）。

    - 所有者保护：对自身 disable → 409 owner_protected，先于一切写入（ADM-AC-020）；
    - disable 递增 token_version（立即撤销既有会话），enable 不递增（旧令牌不复活）；
    - status 同值幂等：无变化不写库、不留审计；改名不递增 token_version（JWT 以 UUID 为锚）；
    - 改名冲突 409 username_taken：业务整体回滚零写入后补记 rejected。
    """
    request_id = audit.request_id_from(request)
    user = await session.get(User, user_id)
    if user is None:
        raise _not_found()
    # 值先取成本地量：回滚后 ORM 对象（含 admin 本人）属性过期，不得再访问
    actor_id = admin.id
    target_id = user.id
    old_username = user.username

    if payload.status == "disabled" and target_id == admin.id:
        await audit.record_rejected(
            actor_id=actor_id,
            target_type=audit.TARGET_USER,
            target_id=target_id,
            action=audit.USER_DISABLE,
            request_id=request_id,
            summary={"status": "disabled"},
        )
        raise _conflict("owner_protected", "所有者账号不可禁用")

    renamed = payload.username is not None and payload.username != old_username
    status_change = (
        payload.status
        if payload.status is not None and payload.status != user.status.value
        else None
    )

    if renamed:
        user.username = payload.username
    if status_change is not None:
        if status_change == "disabled":
            user.token_version += 1
        user.status = UserStatus(status_change)

    if renamed:
        audit.record(
            session,
            actor_id=actor_id,
            target_type=audit.TARGET_USER,
            target_id=target_id,
            action=audit.USER_RENAME,
            request_id=request_id,
            summary={"username": old_username, "new_username": payload.username},
        )
    if status_change is not None:
        audit.record(
            session,
            actor_id=actor_id,
            target_type=audit.TARGET_USER,
            target_id=target_id,
            action=audit.USER_DISABLE if status_change == "disabled" else audit.USER_ENABLE,
            request_id=request_id,
            summary={"status": status_change},
        )

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await audit.record_rejected(
            actor_id=actor_id,
            target_type=audit.TARGET_USER,
            target_id=target_id,
            action=audit.USER_RENAME,
            request_id=request_id,
            summary={"username": old_username, "new_username": payload.username},
        )
        raise _conflict("username_taken", "该用户名已被占用") from None

    row = (await session.execute(_user_with_stats().where(User.id == target_id))).one()
    return _to_out(row)


@router.post("/users/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    user_id: uuid.UUID,
    payload: ResetPasswordInput,
    request: Request,
    admin: RequireAdmin,
    session: SessionDep,
) -> None:
    """重置密码（ADM-FR-008）：置新哈希并递增 token_version，旧会话全部立即失效。

    204 无响应体、不设置任何 Cookie、不回显任何凭据；对自身允许
    （自改密后需重新登录，重新登录后仍为管理员）。审计只记 username，无密码痕迹。
    """
    request_id = audit.request_id_from(request)
    user = await session.get(User, user_id)
    if user is None:
        raise _not_found()
    user.password_hash = hash_password(payload.password)
    user.token_version += 1
    audit.record(
        session,
        actor_id=admin.id,
        target_type=audit.TARGET_USER,
        target_id=user.id,
        action=audit.USER_RESET_PASSWORD,
        request_id=request_id,
        summary={"username": user.username},
    )
    await session.commit()


# ---- 账号永久删除（WI-20261025-03，PRD §6.3/§8.3；ADM-FR-011/012）----


def _op_out(operation: AccountDeletionOperation) -> DeletionOperationOut:
    return DeletionOperationOut.model_validate(operation)


async def _load_operation(
    session: SessionDep, operation_id: uuid.UUID
) -> AccountDeletionOperation:
    operation = await session.get(AccountDeletionOperation, operation_id)
    if operation is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "清理操作不存在"},
        )
    return operation


@router.get("/users/{user_id}/deletion-preview", response_model=AccountDeletionPreview)
async def deletion_preview(
    user_id: uuid.UUID,
    admin: RequireAdmin,
    session: SessionDep,
) -> AccountDeletionPreview:
    """删除预览（ADM-FR-011）：只读聚合，不留审计、不落操作行、不修改任务状态。

    对象列举失败 → 503 storage_unavailable 且不签发凭证（PRD §6.3.1）；
    目标==自身 → 409 owner_protected；活动任务 → blocked 且无可执行 token。
    """
    _require_deletion_enabled()
    if user_id == admin.id:
        raise _conflict("owner_protected", "所有者账号不可删除")
    user = await session.get(User, user_id)
    if user is None:
        raise _not_found()

    # S3 列举不持任何数据库锁；失败快速返回，绝不以 0 冒充无法识别的统计
    try:
        keys = await storage.list_keys(account_deletion.user_prefix(user_id))
    except Exception as exc:  # noqa: BLE001 — 任何列举故障统一按存储不可用处理
        logger.warning("删除预览对象列举失败 user_id=%s error=%s", user_id, type(exc).__name__)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "storage_unavailable", "message": "对象统计暂时不可用，请稍后重试"},
        ) from exc

    counts = await account_deletion.aggregate_counts(session, user_id)
    blocked = await runs.has_active_tasks(session, user_id)
    counts["objects"] = len(keys)
    counts["orphan_objects"] = await account_deletion.orphan_count(session, user_id, keys)

    if blocked:
        return AccountDeletionPreview(
            user_id=user_id,
            username=user.username,
            counts=DeletionCounts(**counts),
            blocked=True,
            blockers=[
                DeletionBlocker(
                    code="active_tasks", message="有任务或计划未结束，请完成或取消后重试"
                )
            ],
            preview_token=None,
            expires_at=None,
        )
    digest = await account_deletion.scope_digest(session, user_id)
    token, expires_at = issue_account_deletion_token(admin.id, user_id, user.username, digest)
    return AccountDeletionPreview(
        user_id=user_id,
        username=user.username,
        counts=DeletionCounts(**counts),
        blocked=False,
        blockers=[],
        preview_token=token,
        expires_at=expires_at,
    )


@router.delete("/users/{user_id}", status_code=status.HTTP_202_ACCEPTED, response_model=DeletionOperationOut)
async def delete_user(
    user_id: uuid.UUID,
    payload: DeletionConfirmInput,
    request: Request,
    admin: RequireAdmin,
    session: SessionDep,
    x_deletion_preview: Annotated[str | None, Header()] = None,
) -> DeletionOperationOut:
    """凭证确认与冻结（ADM-FR-012/025）：校验通过后同事务冻结并建立清理操作。

    先持久化再投递 ARQ；投递失败不回滚（恢复扫描兜底，WI 决策 3）。
    重复提交依据目标已有未终结操作幂等返回原操作。
    """
    _require_deletion_enabled()
    # 凭证校验（锁外）：签名 → 目标绑定 → 确认名与载荷 → 操作者绑定；过期在读取时抛出
    if not x_deletion_preview:
        raise _invalid_token("缺少删除确认凭证")
    try:
        expect = read_account_deletion_token(x_deletion_preview)
    except PreviewTokenExpired as exc:
        raise _conflict(
            "delete_preview_expired", "确认凭证已过期，请刷新预览后重试"
        ) from exc
    except PreviewTokenError as exc:
        raise _invalid_token("删除确认凭证无效") from exc
    if expect.get("t") != str(user_id):
        raise _invalid_token("删除确认凭证与该目标不匹配")
    if payload.confirm_username != expect.get("u"):
        raise _conflict("delete_preview_changed", "确认用户名与预览不一致，请刷新预览后重试")
    if expect.get("o") != str(admin.id):
        raise _invalid_token("删除确认凭证与当前操作者不匹配")

    # 回滚路径需要的值先取成本地量（与既有端点同款防御）
    actor_id = admin.id
    request_id = audit.request_id_from(request)

    # 目标用户临界区：重读 → 核对 → 冻结 + 建操作 + 审计，同事务提交
    await guards.user_lock(session, user_id)
    user = await session.get(User, user_id)
    if user is None:
        raise _not_found()
    if user.username != expect.get("u"):
        raise _conflict("delete_preview_changed", "目标用户名已变化，请刷新预览后重试")
    digest = await account_deletion.scope_digest(session, user_id)
    if digest != expect.get("s"):
        raise _conflict("delete_preview_changed", "删除范围已变化，请刷新预览后重新确认")
    if await runs.has_active_tasks(session, user_id):
        raise _conflict("active_tasks", "有任务或计划未结束，请完成或取消后重试")
    if user_id == actor_id:
        raise _conflict("owner_protected", "所有者账号不可删除")

    existing = await session.scalar(
        select(AccountDeletionOperation).where(
            AccountDeletionOperation.target_user_id == user_id,
            AccountDeletionOperation.status.in_(
                [AccountDeletionStatus.PROCESSING, AccountDeletionStatus.FAILED]
            ),
        )
    )
    if existing is not None:
        # 幂等：同一目标最多一个未终结操作，重复确认只返回原操作
        await session.commit()
        return _op_out(existing)

    user.status = UserStatus.DELETING
    user.token_version += 1
    # id 显式生成：审计行要在 flush 前引用同一 operation_id（FR-023）
    operation_id = uuid.uuid4()
    operation = AccountDeletionOperation(
        id=operation_id,
        target_user_id=user_id,
        actor_id=actor_id,
        status=AccountDeletionStatus.PROCESSING,
        phase=PHASE_FROZEN,
    )
    session.add(operation)
    audit.record(
        session,
        actor_id=actor_id,
        target_type=audit.TARGET_USER,
        target_id=user_id,
        action=audit.USER_DELETE,
        request_id=request_id,
        # summary 不含用户名/对象清单：完成后的最小保留链以此为模板（WI 决策 4e）
        operation_id=operation_id,
    )
    await session.commit()

    try:
        from app.queue import enqueue

        await enqueue("cleanup_account", operation_id)
    except Exception:  # noqa: BLE001 — 投递失败不回滚，恢复扫描每分钟兜底
        logger.warning("清理任务投递失败 operation_id=%s", operation_id, exc_info=True)
    return _op_out(operation)


@router.get("/deletion-operations/{operation_id}", response_model=DeletionOperationOut)
async def get_deletion_operation(
    operation_id: uuid.UUID,
    _admin: RequireAdmin,
    session: SessionDep,
) -> DeletionOperationOut:
    """清理操作详情：不关联用户行，账号删除后仍可查询（PRD §8.1）。"""
    return _op_out(await _load_operation(session, operation_id))


@router.post("/deletion-operations/{operation_id}/retry")
async def retry_deletion_operation(
    operation_id: uuid.UUID,
    request: Request,
    admin: RequireAdmin,
    session: SessionDep,
) -> Response:
    """重试清理（ADM-FR-015/030~033）：仅 failed 重置为 processing 并续投原 ID。

    processing 原样返回 202（不启动并发副本）；succeeded 返回 200 终态。
    retry 不受清理开关限制（FR-027）；failed → processing 留审计 deletion_retry。
    """
    operation = await _load_operation(session, operation_id)
    if operation.status is AccountDeletionStatus.SUCCEEDED:
        return Response(
            content=_op_out(operation).model_dump_json(),
            media_type="application/json",
            status_code=status.HTTP_200_OK,
        )
    if operation.status is AccountDeletionStatus.PROCESSING:
        return Response(
            content=_op_out(operation).model_dump_json(),
            media_type="application/json",
            status_code=status.HTTP_202_ACCEPTED,
        )

    # failed：同 operation_id 断点续跑（phase 保持），不重新生成业务
    operation.status = AccountDeletionStatus.PROCESSING
    operation.attempt_count += 1
    operation.failure_code = None
    operation.failure_detail = None
    audit.record(
        session,
        actor_id=admin.id,
        target_type=audit.TARGET_USER,
        target_id=operation.target_user_id,
        action=audit.DELETION_RETRY,
        request_id=audit.request_id_from(request),
        operation_id=operation.id,
    )
    await session.commit()
    try:
        from app.queue import enqueue

        await enqueue("cleanup_account", operation.id)
    except Exception:  # noqa: BLE001 — 投递失败不回滚，恢复扫描兜底
        logger.warning("清理重试投递失败 operation_id=%s", operation.id, exc_info=True)
    return Response(
        content=_op_out(operation).model_dump_json(),
        media_type="application/json",
        status_code=status.HTTP_202_ACCEPTED,
    )


# ---- 管理端素材（WI-20261025-04，PRD §6.5/§8.1~8.3；ADM-FR-017~020/023）----


# 排序白名单（PRD §8.2）；title 可空，排序时 null 行两方向稳定尾随
_ASSET_SORT_COLUMNS = {
    "created_at": Asset.created_at,
    "size_bytes": Asset.size_bytes,
    "title": Asset.title,
}

# 阻断删除的引用类型（与 services/assets.py::_BLOCKING_TYPES 同口径）
_BLOCKING_REFERENCE_TYPES = frozenset({"original", "current", "layer"})
_REFERENCE_COUNT_TYPES = ("original", "current", "layer", "history")


def _asset_not_found() -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail=DeletionErrorDetail(code="not_found", message="素材不存在").model_dump(
            mode="json", exclude_none=True
        ),
    )


def _asset_conflict(code: str, message: str, references: list | None = None) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail=DeletionErrorDetail(
            code=code, message=message, references=references
        ).model_dump(mode="json", exclude_none=True),
    )


def _asset_q_criterion(needle: str) -> ColumnElement[bool] | None:
    """q 命中规则（PRD §8.2）：title 包含（通配符转义防注入）或完整素材 UUID。"""
    if not needle:
        return None
    try:
        return Asset.id == uuid.UUID(needle)
    except ValueError:
        pattern = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return Asset.title.like(f"%{pattern}%", escape="\\")


def _reference_rows(
    types_by_session: dict[str, set[str]], titles: dict[str, str]
) -> list[ReferenceOut]:
    """详情完整引用行（引用摘要的 references 风格，按会话 id 稳定排序）。"""
    return [
        ReferenceOut(session_id=sid, title=titles.get(sid, ""), types=sorted(types))
        for sid, types in sorted(types_by_session.items())
    ]


def _reference_counts(types_by_session: dict[Any, set[str]]) -> dict[str, int]:
    """引用计数摘要：各类型被多少会话引用（列表与详情同一口径）。"""
    return {
        type_: sum(1 for types in types_by_session.values() if type_ in types)
        for type_ in _REFERENCE_COUNT_TYPES
    }


def _asset_capability(
    asset: Asset,
    owner: User,
    *,
    active_tasks: bool,
    types_by_session: dict[Any, set[str]],
) -> tuple[bool, str | None, bool, str | None]:
    """can_rename/can_delete + 稳定原因码（WI 决策 6）。

    按钮能力只是提示（PRD §6.5），写入时后端仍重算；delete 判定与
    delete_for_user 临界区同序：只读(隐藏) → kind 门禁 → 冻结 → 活动任务 →
    R(X) 阻断；仅历史引用/无引用/仅墙关联均放行。
    """
    rename_blocker = None
    if asset.library_hidden:
        rename_blocker = "asset_readonly"
    elif owner.status is UserStatus.DELETING:
        rename_blocker = "account_deleting"

    delete_blocker = None
    if asset.library_hidden:
        # 隐藏行在 delete_for_user 内不暴露管理入口（404），能力以只读提示
        delete_blocker = "asset_readonly"
    elif asset.kind in UNMANAGEABLE_KINDS:
        delete_blocker = "unsupported_asset_kind"
    elif owner.status is UserStatus.DELETING:
        delete_blocker = "account_deleting"
    elif active_tasks:
        delete_blocker = "active_tasks"
    elif any(types & _BLOCKING_REFERENCE_TYPES for types in types_by_session.values()):
        delete_blocker = "asset_in_use"

    return (
        rename_blocker is None,
        rename_blocker,
        delete_blocker is None,
        delete_blocker,
    )


def _asset_base(
    asset: Asset,
    owner: User,
    *,
    active_tasks: bool,
    types_by_session: dict[Any, set[str]],
) -> AdminAssetOut:
    can_rename, rename_blocker, can_delete, delete_blocker = _asset_capability(
        asset,
        owner,
        active_tasks=active_tasks,
        types_by_session=types_by_session,
    )
    return AdminAssetOut(
        id=asset.id,
        kind=asset.kind,
        source=asset.source,
        title=asset.title,
        image_format=asset.image_format,
        width=asset.width,
        height=asset.height,
        size_bytes=asset.size_bytes,
        has_alpha=asset.has_alpha,
        created_at=asset.created_at,
        user_id=asset.user_id,
        username=owner.username,
        library_hidden=asset.library_hidden,
        references_count=_reference_counts(types_by_session),
        can_rename=can_rename,
        rename_blocker=rename_blocker,
        can_delete=can_delete,
        delete_blocker=delete_blocker,
        preview_url=storage.signed_url(asset.storage_key),
    )


async def _asset_detail(session: SessionDep, asset: Asset) -> AdminAssetDetailOut:
    """详情视图：与列表项同一构造器保证逐字段一致（ADM-AC-036）。"""
    owner = await session.get(User, asset.user_id)
    if owner is None:  # 防御：FK CASCADE 下不应出现
        raise _asset_not_found()
    references = await asset_service.asset_references(session, asset, strict=False)
    base = _asset_base(
        asset,
        owner,
        active_tasks=references["active_tasks"],
        types_by_session=references["types_by_session"],
    )
    return AdminAssetDetailOut(
        **base.model_dump(),
        references=_reference_rows(
            references["types_by_session"], references["session_titles"]
        ),
    )


@router.get("/assets", response_model=Page[AdminAssetOut])
async def list_assets(
    params: Annotated[AdminAssetListParams, Query()],
    _admin: RequireAdmin,
    session: SessionDep,
) -> Page[AdminAssetOut]:
    """素材列表（ADM-FR-017，WI 决策 1）：跨用户全量（含隐藏与内部派生）+
    user_id/q/kind/source/library_hidden/时间区间筛选 + 白名单排序（title 的
    null 行两方向稳定尾随）。join users 仅取归属（一对一，不抬高 total）；
    同 owner 的引用扫描与活动任务在页内缓存一次；普通读取不产生审计行，
    preview_url 为短期签名 URL（不入审计）。"""
    stmt = select(Asset, User).join(User, User.id == Asset.user_id)
    if params.user_id is not None:
        stmt = stmt.where(Asset.user_id == params.user_id)
    if params.q is not None:
        criterion = _asset_q_criterion(params.q.strip())
        if criterion is not None:
            stmt = stmt.where(criterion)
    if params.kind is not None:
        stmt = stmt.where(Asset.kind == params.kind)
    if params.source is not None:
        stmt = stmt.where(Asset.source == params.source)
    if params.library_hidden is not None:
        stmt = stmt.where(Asset.library_hidden.is_(params.library_hidden))
    if params.created_from is not None:
        stmt = stmt.where(Asset.created_at >= params.created_from)
    if params.created_to is not None:
        # 区间上界不包含（PRD §8.2）
        stmt = stmt.where(Asset.created_at < params.created_to)

    rows, total = await paging.paginate(
        session, stmt, params, _ASSET_SORT_COLUMNS, nulls_last=("title",)
    )

    # 同 owner 的引用扫描与活动任务只算一次（页 ≤100 行）
    scans: dict[uuid.UUID, dict] = {}
    tasks: dict[uuid.UUID, bool] = {}
    items = []
    for row in rows:
        asset, owner = row.Asset, row.User
        if owner.id not in scans:
            scans[owner.id] = await asset_service.scan_user_references(
                session, owner.id, strict=False
            )
            tasks[owner.id] = await runs.has_active_tasks(session, owner.id)
        entry = scans[owner.id]["assets"].get(asset.id) or {
            "types": {},
            "history_docs": 0,
        }
        items.append(
            _asset_base(
                asset,
                owner,
                active_tasks=tasks[owner.id],
                types_by_session=entry["types"],
            )
        )
    return Page(
        items=items, total=total, page=params.page, page_size=params.page_size
    )


@router.post("/assets/batch-delete", response_model=BatchDeleteOut)
async def batch_delete_assets(
    payload: BatchDeleteIn,
    request: Request,
    admin: RequireAdmin,
    session: SessionDep,
) -> BatchDeleteOut:
    """跨用户批删（ADM-FR-020，WI 决策 5）：原始输入 1~100（422），去重保序后
    请求级全量核验——任一缺失整批 404 零写入且不回显具体 ID；通过后逐项独立
    事务按各自 owner 调 delete_for_user（保护逐项重算，冲突计 skipped），每个
    owner 收尾扫尾一次。每实际删除项独立补记 asset_delete（skipped 不逐条
    留痕）；突发异常停止后续项并整体 500（已提交项不回滚，detail 附部分结果）。"""
    request_id = audit.request_id_from(request)
    actor_id = admin.id
    unique = list(dict.fromkeys(payload.asset_ids))

    rows = (
        await session.execute(
            select(Asset.id, Asset.user_id, Asset.kind).where(Asset.id.in_(unique))
        )
    ).all()
    owners = {row[0]: (row[1], row[2]) for row in rows}
    if len(owners) != len(unique):
        raise _asset_not_found()
    owner_rows = (
        await session.execute(
            select(User.id, User.status).where(
                User.id.in_({user_id for user_id, _ in owners.values()})
            )
        )
    ).all()
    frozen = {row[0] for row in owner_rows if row[1] is UserStatus.DELETING}

    deleted: list[uuid.UUID] = []
    skipped: list[BatchSkippedItem] = []
    swept_owner: uuid.UUID | None = None

    async def sweep(owner_id: uuid.UUID) -> None:
        for swept_id, key in await asset_service.sweep_hidden_for_user(session, owner_id):
            await asset_service.cleanup_object(key, swept_id)

    try:
        for asset_id in unique:
            owner_id, kind = owners[asset_id]
            if swept_owner is not None and owner_id != swept_owner:
                await sweep(swept_owner)
                swept_owner = None
            if owner_id in frozen:
                skipped.append(
                    BatchSkippedItem(
                        id=asset_id,
                        code="account_deleting",
                        reason="目标账号删除中，素材变更被拒绝",
                    )
                )
                continue
            try:
                reclaimed = await asset_service.delete_for_user(session, owner_id, asset_id)
            except asset_service.AssetNotFound:
                skipped.append(
                    BatchSkippedItem(
                        id=asset_id, code="not_found", reason="记录已不存在或已隐藏"
                    )
                )
                continue
            except asset_service.UnsupportedAssetKind:
                skipped.append(
                    BatchSkippedItem(
                        id=asset_id,
                        code="unsupported_asset_kind",
                        reason="会话内部派生素材，本期不支持删除",
                    )
                )
                continue
            except ActiveTasks as exc:
                skipped.append(
                    BatchSkippedItem(
                        id=asset_id,
                        code="active_tasks",
                        reason=str(exc) or "有任务或计划未结束，请完成或取消后重试",
                    )
                )
                continue
            except asset_service.AssetInUse as exc:
                skipped.append(
                    BatchSkippedItem(
                        id=asset_id,
                        code="asset_in_use",
                        reason=asset_service.in_use_reason(exc.references),
                    )
                )
                continue
            except ReferenceStateInvalid:
                skipped.append(
                    BatchSkippedItem(
                        id=asset_id,
                        code="reference_state_invalid",
                        reason="引用数据无法安全解析，需修复后才能删除",
                    )
                )
                continue
            deleted.append(asset_id)
            # 每项提交后即清理对象；失败仅告警（LIB-FR-017 语义不变）
            for swept_id, key in reclaimed:
                await asset_service.cleanup_object(key, swept_id)
            hidden = not any(swept == asset_id for swept, _ in reclaimed)
            await audit.record_committed(
                actor_id=actor_id,
                target_type=audit.TARGET_ASSET,
                target_id=asset_id,
                action=audit.ASSET_DELETE,
                request_id=request_id,
                summary={
                    "kind": kind.value,
                    "reason": "仅历史引用，已转为隐藏" if hidden else "已删除",
                },
            )
            swept_owner = owner_id
        if swept_owner is not None:
            await sweep(swept_owner)
    except Exception as exc:
        # 突发异常（WI 决策 5）：停止后续项，已提交项不回滚，500 附部分结果说明
        logger.warning("管理端批删中断 deleted=%s", deleted, exc_info=True)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "batch_interrupted",
                "message": "批量删除中断，已删除项保留，请刷新确认部分结果",
                "deleted": [str(item) for item in deleted],
                "skipped": [item.model_dump(mode="json") for item in skipped],
            },
        ) from exc

    return BatchDeleteOut(deleted=deleted, skipped=skipped)


@router.get("/assets/{asset_id}", response_model=AdminAssetDetailOut)
async def get_asset(
    asset_id: uuid.UUID,
    _admin: RequireAdmin,
    session: SessionDep,
) -> AdminAssetDetailOut:
    """素材详情（ADM-FR-017，WI 决策 2）：完整 references（原图/当前图/图层/
    历史/墙拓扑）+ 引用计数摘要 + 能力原因码 + 短期签名 preview_url（不入审计）。"""
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise _asset_not_found()
    return await _asset_detail(session, asset)


@router.patch("/assets/{asset_id}", response_model=AdminAssetDetailOut)
async def rename_asset(
    asset_id: uuid.UUID,
    payload: AdminAssetPatchIn,
    request: Request,
    admin: RequireAdmin,
    session: SessionDep,
) -> AdminAssetDetailOut:
    """管理员重命名（ADM-FR-018，WI 决策 3）：title 规则沿用用户端口径
    （trim、空白归一 null、≤80 码点，超长 422）；隐藏（asset_readonly）或
    所有者冻结（account_deleting）→ 409；SNAPSHOT 与内部派生 kind 均可命名
    （不因管理员身份扩大删除权限）。审计 asset_rename 同事务留痕。"""
    request_id = audit.request_id_from(request)
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise _asset_not_found()
    owner = await session.get(User, asset.user_id)
    if owner is None:
        raise _asset_not_found()
    if asset.library_hidden:
        raise _asset_conflict("asset_readonly", "隐藏素材只读，不可改名")
    if owner.status is UserStatus.DELETING:
        raise _asset_conflict("account_deleting", "目标账号删除中，素材变更被拒绝")

    old_title = asset.title
    asset.title = payload.title
    audit.record(
        session,
        actor_id=admin.id,
        target_type=audit.TARGET_ASSET,
        target_id=asset.id,
        action=audit.ASSET_RENAME,
        request_id=request_id,
        summary={"title": old_title, "new_title": payload.title},
    )
    await session.commit()
    return await _asset_detail(session, asset)


@router.delete("/assets/{asset_id}", response_model=AdminAssetDeleteOut)
async def delete_asset(
    asset_id: uuid.UUID,
    request: Request,
    admin: RequireAdmin,
    session: SessionDep,
) -> AdminAssetDeleteOut:
    """素材单删（ADM-FR-019，WI 决策 4）：直接调用原所有者的
    delete_for_user（复用临界区与全部保护），路由层把返回语义映射为管理端
    两态响应（deleted=物理删除 / hidden=仅历史引用转隐藏），409 沿用
    references 风格 blockers；对象清理由路由在提交后执行（失败仅告警）。
    所有者冻结期拒绝素材变更（PRD §7）；删除成功独立补记 asset_delete。"""
    request_id = audit.request_id_from(request)
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise _asset_not_found()
    owner = await session.get(User, asset.user_id)
    if owner is None:
        raise _asset_not_found()
    kind = asset.kind
    if owner.status is UserStatus.DELETING:
        raise _asset_conflict("account_deleting", "目标账号删除中，素材变更被拒绝")

    try:
        reclaimed = await asset_service.delete_for_user(session, asset.user_id, asset_id)
    except asset_service.AssetNotFound as exc:
        # 隐藏行不暴露管理删除入口（404 不区分不存在与非存在），LIB-FR-011
        raise _asset_not_found() from exc
    except asset_service.UnsupportedAssetKind as exc:
        raise _asset_conflict(
            "unsupported_asset_kind", "会话内部派生素材，本期不支持删除"
        ) from exc
    except ActiveTasks as exc:
        raise _asset_conflict(
            "active_tasks", str(exc) or "有任务或计划未结束，请完成或取消后重试"
        ) from exc
    except asset_service.AssetInUse as exc:
        raise _asset_conflict(
            "asset_in_use",
            "素材仍被会话的原图、当前图或图层引用，请先删除对应会话",
            exc.references,
        ) from exc
    except ReferenceStateInvalid as exc:
        raise _asset_conflict(
            "reference_state_invalid", "引用数据无法安全解析，请先修复数据后再删除"
        ) from exc

    hidden = not any(swept == asset_id for swept, _ in reclaimed)
    for swept_id, key in reclaimed:
        await asset_service.cleanup_object(key, swept_id)
    await audit.record_committed(
        actor_id=admin.id,
        target_type=audit.TARGET_ASSET,
        target_id=asset_id,
        action=audit.ASSET_DELETE,
        request_id=request_id,
        summary={
            "kind": kind.value,
            "reason": "仅历史引用，已转为隐藏" if hidden else "已删除",
        },
    )
    return AdminAssetDeleteOut(id=asset_id, status="hidden" if hidden else "deleted")


# ---- 任务与操作查询（WI-20261025-05，PRD §6.5/§8.1~8.3；ADM-FR-021/022/024）----
#
# 只读红线（WI 决策 1/6）：全部直接 select / session.get，绝不经过 runs.get、
# expire_stale 等会落库发事件的路径；读取零审计（不调用 audit.record）；不注册
# 任何写方法（PUT/PATCH/DELETE 未注册 → 落 /api 兜底 404）。params/goal/reply/
# 原始 JSON 一律不透传（WI 决策 2/3），错误摘要经 _sanitize_error（WI 决策 5）。


_TOOL_RUN_SORT_COLUMNS = {
    "created_at": ToolRun.created_at,
    "status": ToolRun.status,
    "tool": ToolRun.tool,
}
_AGENT_RUN_SORT_COLUMNS = {
    "created_at": AgentRun.created_at,
    "status": AgentRun.status,
}
_AUDIT_LOG_SORT_COLUMNS = {
    "created_at": AdminAuditLog.created_at,
    "action": AdminAuditLog.action,
    "result": AdminAuditLog.result,
}

_ERROR_SUMMARY_MAX = 200
_ERROR_FALLBACK = "（原始错误详情已脱敏）"
# 请求/响应/头转储行：整行丢弃（WI 决策 5：剔除而非仅截断）
_DUMP_LINE_RE = re.compile(r"(?i)^(request|response|headers|body)\b\s*[:：]")
# 原始 response body 等 JSON 对象：由内向外逐层剥离
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}")
# 签名 URL / 带查询串的 URL：整体剔除
_URL_RE = re.compile(r"https?://\S+")
# Authorization / api key 形态键值：键值整体剔除
_SECRET_KV_RE = re.compile(
    r"(?i)\b(authorization|x-api-key|api[-_]?key|access[-_]?key|secret[-_]?key"
    r"|access[-_]?token|bearer)\b\s*[:=]?\s*\S+"
)


def _run_not_found() -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND, detail={"code": "not_found", "message": "任务不存在"}
    )


def _turn_not_found() -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND, detail={"code": "not_found", "message": "轮次不存在"}
    )


def _audit_log_not_found() -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND, detail={"code": "not_found", "message": "操作记录不存在"}
    )


def _sanitize_error(error: str | None) -> str | None:
    """错误摘要脱敏（WI 决策 5 / ADM-AC-045）：剔除 URL 查询串与签名 URL、
    Authorization/api key 形态键值、原始 response body（JSON 对象整体剥离），
    请求/响应转储行整行丢弃；只留异常类型名 + 截断消息（≤200 字符、去除换行）。
    全部剔除后返回固定占位，保证摘要非空。"""
    if not error:
        return None
    cleaned = error
    while True:
        stripped = _JSON_OBJECT_RE.sub(" ", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _SECRET_KV_RE.sub(" ", cleaned)
    kept = [
        line.strip()
        for line in cleaned.splitlines()
        if line.strip() and not _DUMP_LINE_RE.match(line.strip())
    ]
    summary = "；".join(kept)[:_ERROR_SUMMARY_MAX]
    return summary or _ERROR_FALLBACK


def _duration_ms(started_at: datetime | None, finished_at: datetime | None) -> int | None:
    """耗时仅当起止均存在才计算（WI 决策 2），否则 null 不编造。"""
    if started_at is None or finished_at is None:
        return None
    return int((finished_at - started_at).total_seconds() * 1000)


def _steps_summary(plan: list | None) -> list[dict]:
    """plan 步骤摘要（WI 决策 3）：每步仅 tool 名与 status。"""
    if not isinstance(plan, list):
        return []
    return [
        {"tool": str(step["tool"]), "status": str(step.get("status") or "")}
        for step in plan
        if isinstance(step, dict) and step.get("tool")
    ]


def _products_of_result(result: dict | None) -> list:
    """一次任务的产物 asset_id（与 services.agent._products_of 同口径）：
    result.asset_ids 与批量 items[].output_ids 统一。"""
    result = result or {}
    products = list(result.get("asset_ids") or [])
    if not products:
        for item in result.get("items") or []:
            if isinstance(item, dict) and item.get("status") == "succeeded":
                products.extend(item.get("output_ids") or [])
    return products


async def _artifact_entries(
    session: AsyncSession, asset_ids: list
) -> list[dict[str, Any]]:
    """产物条目（WI 决策 2/5）：素材行存在 → {asset_id, kind, url(短期签名)}；
    行已删 → {asset_id, missing: true}，不使详情失败、不给签名 URL。"""
    unique = list(dict.fromkeys(str(a) for a in asset_ids if a is not None))
    if not unique:
        return []
    rows = (
        await session.execute(
            select(Asset.id, Asset.kind, Asset.storage_key).where(
                Asset.id.in_([uuid.UUID(a) for a in unique])
            )
        )
    ).all()
    found = {row[0]: (row[1], row[2]) for row in rows}
    entries: list[dict[str, Any]] = []
    for raw in unique:
        asset_id = uuid.UUID(raw)
        info = found.get(asset_id)
        if info is None:
            entries.append({"asset_id": asset_id, "missing": True})
        else:
            kind, storage_key = info
            entries.append(
                {
                    "asset_id": asset_id,
                    "kind": kind.value,
                    "url": storage.signed_url(storage_key),
                }
            )
    return entries


def _tool_run_out(run: ToolRun, username: str | None) -> AdminToolRunOut:
    return AdminToolRunOut(
        id=run.id,
        user_id=run.user_id,
        username=username or "",
        session_id=run.session_id,
        tool=run.tool,
        status=run.status,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        heartbeat_at=run.heartbeat_at,
        duration_ms=_duration_ms(run.started_at, run.finished_at),
    )


def _audit_log_out(log: AdminAuditLog, username: str | None) -> AdminAuditLogOut:
    return AdminAuditLogOut(
        id=log.id,
        actor_id=log.actor_id,
        actor_username=username,
        target_type=log.target_type,
        target_id=log.target_id,
        action=log.action,
        result=log.result,
        request_id=log.request_id,
        summary=log.summary,
        operation_id=log.operation_id,
        created_at=log.created_at,
    )


# 会话内轮次序号的关联子查询：AgentRun 无持久化轮次列，按既有 (created_at, id)
# 双键次序（与 services.agent.turns_of 同口径）如实推算 1 基序数（WI 决策 3）
_PeerAgentRun = aliased(AgentRun)


def _turn_ordinal() -> Any:
    return (
        select(func.count())
        .select_from(_PeerAgentRun)
        .where(
            _PeerAgentRun.session_id == AgentRun.session_id,
            tuple_(_PeerAgentRun.created_at, _PeerAgentRun.id)
            <= tuple_(AgentRun.created_at, AgentRun.id),
        )
        .correlate(AgentRun)
        .scalar_subquery()
        .label("turn")
    )


async def _results_of(
    session: AsyncSession, run_ids: set[uuid.UUID]
) -> dict[uuid.UUID, dict | None]:
    """页内/轮次内关联工具任务的 result 一次取回（避免逐行查询）。"""
    if not run_ids:
        return {}
    return dict(
        (
            await session.execute(
                select(ToolRun.id, ToolRun.result).where(ToolRun.id.in_(run_ids))
            )
        ).all()
    )


def _plan_products(
    plan: list | None, results: dict[uuid.UUID, dict | None]
) -> list[uuid.UUID]:
    """plan 关联工具任务的产物 ID（去重保序）。"""
    products: list[uuid.UUID] = []
    for step in plan or []:
        if not isinstance(step, dict) or not step.get("run_id"):
            continue
        try:
            run_id = uuid.UUID(str(step["run_id"]))
        except ValueError:
            continue
        for product in _products_of_result(results.get(run_id)):
            product_id = uuid.UUID(str(product))
            if product_id not in products:
                products.append(product_id)
    return products


@router.get("/tool-runs", response_model=Page[AdminToolRunOut])
async def list_tool_runs(
    params: Annotated[AdminToolRunListParams, Query()],
    _admin: RequireAdmin,
    session: SessionDep,
) -> Page[AdminToolRunOut]:
    """工具任务列表（ADM-FR-021/022，WI 决策 1/2/6）：跨用户全量 +
    user_id/status/tool/created_from~to（下含上不含）筛选 + 白名单排序
    （id 同方向稳定）。直接 select——绝不经过 runs.get 的失联过期路径；
    读取零审计；params/result 原始 JSON 不透传。"""
    stmt = select(ToolRun, User.username).join(User, User.id == ToolRun.user_id)
    if params.user_id is not None:
        stmt = stmt.where(ToolRun.user_id == params.user_id)
    if params.status is not None:
        stmt = stmt.where(ToolRun.status == params.status)
    if params.tool is not None:
        stmt = stmt.where(ToolRun.tool == params.tool)
    if params.created_from is not None:
        stmt = stmt.where(ToolRun.created_at >= params.created_from)
    if params.created_to is not None:
        # 区间上界不包含（PRD §8.2）
        stmt = stmt.where(ToolRun.created_at < params.created_to)
    rows, total = await paging.paginate(session, stmt, params, _TOOL_RUN_SORT_COLUMNS)
    return Page(
        items=[_tool_run_out(row.ToolRun, row.username) for row in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/tool-runs/{run_id}", response_model=AdminToolRunDetailOut)
async def get_tool_run(
    run_id: uuid.UUID,
    _admin: RequireAdmin,
    session: SessionDep,
) -> AdminToolRunDetailOut:
    """工具任务详情（ADM-FR-021/022/043/045）：白名单字段 + duration + 步骤
    （工具任务无则空）+ 产物（缺失标记 missing）+ 脱敏 error；session.get
    只读，不触发失联过期；params/result 原始 JSON 不透传。"""
    run = await session.get(ToolRun, run_id)
    if run is None:
        raise _run_not_found()
    username = await session.scalar(select(User.username).where(User.id == run.user_id))
    return AdminToolRunDetailOut(
        **_tool_run_out(run, username).model_dump(),
        steps=[],  # 工具任务无计划步骤（WI 决策 2：无则空）
        artifacts=await _artifact_entries(session, _products_of_result(run.result)),
        error=_sanitize_error(run.error),
    )


@router.get("/agent-runs", response_model=Page[AdminAgentRunOut])
async def list_agent_runs(
    params: Annotated[AdminAgentRunListParams, Query()],
    _admin: RequireAdmin,
    session: SessionDep,
) -> Page[AdminAgentRunOut]:
    """Agent 轮次列表（ADM-FR-021/022，WI 决策 1/3/6）：跨用户全量 +
    user_id/status/session_id/created_from~to 筛选 + 白名单排序（created_at/
    status，无 tool）；turn 为会话内轮次序号；goal/reply/原始 JSON 不透传，
    时间只有 created_at 不编造起止耗时；直接 select 零副作用零审计。"""
    stmt = select(AgentRun, User.username, _turn_ordinal()).join(
        User, User.id == AgentRun.user_id
    )
    if params.user_id is not None:
        stmt = stmt.where(AgentRun.user_id == params.user_id)
    if params.status is not None:
        stmt = stmt.where(AgentRun.status == params.status)
    if params.session_id is not None:
        stmt = stmt.where(AgentRun.session_id == params.session_id)
    if params.created_from is not None:
        stmt = stmt.where(AgentRun.created_at >= params.created_from)
    if params.created_to is not None:
        stmt = stmt.where(AgentRun.created_at < params.created_to)
    rows, total = await paging.paginate(session, stmt, params, _AGENT_RUN_SORT_COLUMNS)

    # 页内产物一次性解析（≤100 行）：plan 关联 run_id → tool_runs.result → assets
    linked_ids = {
        uuid.UUID(str(step["run_id"]))
        for row in rows
        for step in row.AgentRun.plan or []
        if isinstance(step, dict) and step.get("run_id")
    }
    results = await _results_of(session, linked_ids)
    entries = {
        entry["asset_id"]: entry
        for entry in await _artifact_entries(
            session,
            [a for result in results.values() for a in _products_of_result(result)],
        )
    }
    items = [
        AdminAgentRunOut(
            id=row.AgentRun.id,
            user_id=row.AgentRun.user_id,
            username=row.username,
            session_id=row.AgentRun.session_id,
            status=row.AgentRun.status,
            created_at=row.AgentRun.created_at,
            turn=row.turn,
            steps=_steps_summary(row.AgentRun.plan),
            artifacts=[
                entries[pid]
                for pid in _plan_products(row.AgentRun.plan, results)
                if pid in entries
            ],
        )
        for row in rows
    ]
    return Page(items=items, total=total, page=params.page, page_size=params.page_size)


@router.get("/agent-runs/{turn_id}", response_model=AdminAgentRunDetailOut)
async def get_agent_run(
    turn_id: uuid.UUID,
    _admin: RequireAdmin,
    session: SessionDep,
) -> AdminAgentRunDetailOut:
    """Agent 轮次详情（ADM-FR-021/022/043/045）：步骤摘要（每步仅 tool 与
    status）+ 同轮次关联工具任务的产物（缺失标记）+ 脱敏 error；turn 序号与
    时间字段口径同列表；goal/reply/params 不透传；session.get 只读。"""
    turn = await session.get(AgentRun, turn_id)
    if turn is None:
        raise _turn_not_found()
    username = await session.scalar(select(User.username).where(User.id == turn.user_id))
    ordinal = await session.scalar(
        select(func.count())
        .select_from(AgentRun)
        .where(
            AgentRun.session_id == turn.session_id,
            tuple_(AgentRun.created_at, AgentRun.id)
            <= tuple_(turn.created_at, turn.id),
        )
    )
    run_ids = {
        uuid.UUID(str(step["run_id"]))
        for step in turn.plan or []
        if isinstance(step, dict) and step.get("run_id")
    }
    results = await _results_of(session, run_ids)
    return AdminAgentRunDetailOut(
        id=turn.id,
        user_id=turn.user_id,
        username=username or "",
        session_id=turn.session_id,
        status=turn.status,
        created_at=turn.created_at,
        turn=ordinal,
        steps=_steps_summary(turn.plan),
        artifacts=await _artifact_entries(session, _plan_products(turn.plan, results)),
        error=_sanitize_error(turn.error),
    )


@router.get("/audit-logs", response_model=Page[AdminAuditLogOut])
async def list_audit_logs(
    params: Annotated[AdminAuditLogListParams, Query()],
    _admin: RequireAdmin,
    session: SessionDep,
) -> Page[AdminAuditLogOut]:
    """操作记录列表（ADM-FR-024，WI 决策 4/6）：actor_id/target_id/action/
    result/created_from~to 筛选 + 白名单排序；无筛选默认不截断（total 为全表
    真实总数）；actor LEFT JOIN（已删为 null）；读取零审计；不提供任何写端点。"""
    stmt = select(AdminAuditLog, User.username).outerjoin(
        User, User.id == AdminAuditLog.actor_id
    )
    if params.actor_id is not None:
        stmt = stmt.where(AdminAuditLog.actor_id == params.actor_id)
    if params.target_id is not None:
        stmt = stmt.where(AdminAuditLog.target_id == params.target_id)
    if params.action is not None:
        stmt = stmt.where(AdminAuditLog.action == params.action)
    if params.result is not None:
        stmt = stmt.where(AdminAuditLog.result == params.result)
    if params.created_from is not None:
        stmt = stmt.where(AdminAuditLog.created_at >= params.created_from)
    if params.created_to is not None:
        # 区间上界不包含（PRD §8.2）
        stmt = stmt.where(AdminAuditLog.created_at < params.created_to)
    rows, total = await paging.paginate(session, stmt, params, _AUDIT_LOG_SORT_COLUMNS)
    return Page(
        items=[_audit_log_out(row.AdminAuditLog, row.username) for row in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/audit-logs/{log_id}", response_model=AdminAuditLogDetailOut)
async def get_audit_log(
    log_id: uuid.UUID,
    _admin: RequireAdmin,
    session: SessionDep,
) -> AdminAuditLogDetailOut:
    """操作记录详情（ADM-FR-024/048）：全字段 + actor join（已删 null）+
    operation_id 非空时嵌套清理操作视图（读不受清理开关限制、不含 scope_digest
    原始对象清单）；404 不区分不存在原因。"""
    row = (
        await session.execute(
            select(AdminAuditLog, User.username)
            .outerjoin(User, User.id == AdminAuditLog.actor_id)
            .where(AdminAuditLog.id == log_id)
        )
    ).first()
    if row is None:
        raise _audit_log_not_found()
    operation = None
    if row.AdminAuditLog.operation_id is not None:
        record = await session.get(AccountDeletionOperation, row.AdminAuditLog.operation_id)
        if record is not None:
            operation = DeletionOperationOut.model_validate(record)
    return AdminAuditLogDetailOut(
        **_audit_log_out(row.AdminAuditLog, row.username).model_dump(),
        operation=operation,
    )
