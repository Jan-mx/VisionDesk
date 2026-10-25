"""管理端用户维护端点（WI-20261025-02 实现要求 4~8，PRD §8）。

全部端点先经 RequireAdmin 鉴权再访问目标；静态路径注册在 /{id} 之前；
错误结构沿用 detail.code/message，404 不区分不存在原因。
统计聚合用相关子查询（不产生 JOIN，total 不被抬高）；写动作与审计同事务，
预期拒绝在业务回滚后另起事务补记 rejected（services/audit.py）。
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.exc import IntegrityError

from app.db import SessionDep
from app.deps import RequireAdmin
from app.models import AgentRun, Asset, EditSession, ToolRun, User
from app.models.user import UserStatus
from app.schemas.admin import AdminUserOut, AdminUserPatch, ResetPasswordInput, UserListParams
from app.schemas.auth import Credentials
from app.security import hash_password
from app.services import audit, paging
from app.services.paging import Page

router = APIRouter(prefix="/admin", tags=["admin"])

# 排序白名单（PRD §8.2：sort 只能来自资源白名单，不接受任意 SQL 路径）
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
