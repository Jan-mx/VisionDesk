import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.models.user import UserStatus
from app.security import hash_password, verify_password


class UsernameTaken(Exception):
    pass


class InvalidCredentials(Exception):
    pass


async def register(session: AsyncSession, username: str, password: str) -> User:
    user = User(username=username, password_hash=hash_password(password))
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise UsernameTaken from exc
    return user


async def authenticate(session: AsyncSession, username: str, password: str) -> User:
    user = await session.scalar(select(User).where(User.username == username))
    # 禁用/删除中账号与错误密码统一走通用认证失败，不泄漏账号状态（PRD §6.2）
    if (
        user is None
        or user.status != UserStatus.ACTIVE
        or not verify_password(password, user.password_hash)
    ):
        raise InvalidCredentials
    # 登录成功才记 last_login_at，与查询同一事务提交（WI-20261025-01 决策 5）
    user.last_login_at = datetime.now(UTC)
    await session.commit()
    return user


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)
