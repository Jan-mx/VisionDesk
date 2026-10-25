"""受控管理员初始化命令（PRD §6.2 / §9、WI-20261025-01 决策 7）。

用法::

    python -m app.init_admin <username>

- 显式指定已有账号提升为管理员；全库最多一位（部分唯一索引 uq_users_single_admin 兜底）。
- 目标已是管理员：幂等成功。
- 用户不存在或已有其他管理员：明确报错、非零退出，绝不隐式转移所有者。
- 不收密码、不输出凭据、不做注册顺序推断。
"""

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionFactory
from app.models import User
from app.models.user import UserRole


class AdminInitError(Exception):
    """提升失败：用户不存在或已有其他管理员。"""


async def promote_admin(session: AsyncSession, username: str) -> User:
    """把指定用户提升为唯一管理员；变更由调用方提交。

    幂等：目标已是 admin 时直接返回，不做任何写操作。
    """
    user = await session.scalar(select(User).where(User.username == username))
    if user is None:
        raise AdminInitError(f"用户 {username} 不存在")
    if user.role == UserRole.ADMIN:
        return user
    existing = await session.scalar(select(User).where(User.role == UserRole.ADMIN))
    if existing is not None:
        raise AdminInitError(f"已存在其他管理员 {existing.username}，拒绝转移所有者")
    user.role = UserRole.ADMIN
    return user


async def _main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("用法: python -m app.init_admin <username>", file=sys.stderr)
        return 2
    username = argv[0]
    async with SessionFactory() as session:
        try:
            await promote_admin(session, username)
        except AdminInitError as exc:
            await session.rollback()
            print(f"init_admin 失败: {exc}", file=sys.stderr)
            return 1
        await session.commit()
    # 只输出用户名与结果，不涉及任何凭据
    print(f"已将 {username} 提升为管理员")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
