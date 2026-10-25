"""管理端用户维护端点（WI-20261025-02 实现要求 4~8；阶段 3 账号删除 WI-20261025-03）。

全部端点先经 RequireAdmin 鉴权再访问目标；静态路径注册在 /{id} 之前；
错误结构沿用 detail.code/message，404 不区分不存在原因。
统计聚合用相关子查询（不产生 JOIN，total 不被抬高）；写动作与审计同事务，
预期拒绝在业务回滚后另起事务补记 rejected（services/audit.py）。

账号删除（WI-20261025-03 决策 3）：开关关闭时 deletion-preview 与 DELETE 均
503 deletion_unavailable、零写入零审计；确认校验序为 签名/目标(422) → 确认名
与凭证载荷(409) → 操作者(422) → 过期(409) → 目标用户锁内 重读用户名/重算范围
摘要(409) / 活动任务(409) / 所有者保护(409) → 已有未终结操作幂等返回(202)。
"""

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.exc import IntegrityError

from app import storage
from app.config import get_settings
from app.db import SessionDep
from app.deps import RequireAdmin
from app.models import AgentRun, Asset, EditSession, ToolRun, User
from app.models.account_deletion_operation import (
    PHASE_FROZEN,
    AccountDeletionOperation,
    AccountDeletionStatus,
)
from app.models.user import UserStatus
from app.schemas.admin import (
    AccountDeletionPreview,
    AdminUserOut,
    AdminUserPatch,
    DeletionConfirmInput,
    DeletionCounts,
    DeletionOperationOut,
    ResetPasswordInput,
    UserListParams,
)
from app.schemas.auth import Credentials
from app.schemas.deletion import DeletionBlocker
from app.security import (
    PreviewTokenError,
    PreviewTokenExpired,
    hash_password,
    issue_account_deletion_token,
    read_account_deletion_token,
)
from app.services import account_deletion, audit, guards, paging, runs
from app.services.paging import Page

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
