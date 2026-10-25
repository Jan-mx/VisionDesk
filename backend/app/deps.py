from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status

from app.db import SessionDep
from app.models import User
from app.models.user import UserStatus, UserRole
from app.security import SESSION_COOKIE, read_token
from app.services import auth as auth_service

_UNAUTHENTICATED = HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录或会话已过期")
# 统一 401，不区分「禁用 / 版本失效 / 令牌非法」，不向客户端泄漏账号状态细节
_FORBIDDEN = HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")


async def current_user(
    session: SessionDep,
    token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User:
    if token is None:
        raise _UNAUTHENTICATED

    parsed = read_token(token)
    if parsed is None:
        raise _UNAUTHENTICATED
    user_id, ver = parsed

    user = await auth_service.get_by_id(session, user_id)
    # 每次请求读取最新状态：禁用/删除中或会话版本过期（改密、撤销）一律 401（PRD §6.2）
    if (
        user is None
        or user.status != UserStatus.ACTIVE
        or ver != user.token_version
    ):
        raise _UNAUTHENTICATED
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def require_admin(user: CurrentUser) -> User:
    """管理端点依赖：未认证 401（由 current_user 抛出），已认证非管理员 403。"""
    if user.role != UserRole.ADMIN:
        raise _FORBIDDEN
    return user


RequireAdmin = Annotated[User, Depends(require_admin)]
