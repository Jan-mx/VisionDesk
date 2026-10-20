"""跨进程并发协调：用户级事务咨询锁 + worker 执行代次写入资格校验。

WI-20261020-01 决策 1/2 的落点：
- 锁：``pg_advisory_xact_lock(920620, hashtext(user_id::text))``，事务级、commit 自动释放。
  命名空间常量 920620 全库唯一，仅用于「同用户 登记侧/写侧 vs 删除侧」互斥。
- 临界区形状：单事务「取锁 → 校验 → 写入 → commit」。**禁止在持锁期间调用外部模型或
  任意会再次 commit 的服务函数**——事务级咨询锁会随任何一次 commit 提前释放，
  因此持锁函数（runs.create / sessions.create / sessions.apply_edit /
  assets.create_from_bytes / runs.finish / 三个删除事务 / guards.critical 块）
  一律不互相调用，锁只在各自事务内存在。
- 执行代次：复用 tool_runs.retries（runs.start 每次执行自增）。tools.execute 在
  start 后捕获当前值，经 current_execution contextvar 在同一次 job 内传递；
  写入资格 = 行存在 ∧ status=RUNNING ∧ retries 与捕获值一致。
"""

import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# WI 决策 1 固定的咨询锁命名空间（全库唯一，勿复用）
LOCK_NAMESPACE = 920620

# (run_id, retries)：当前 worker 执行的代次；None 表示 API 直写路径，不做资格校验
current_execution: ContextVar[tuple[uuid.UUID, int] | None] = ContextVar(
    "current_execution", default=None
)


class StaleExecution(Exception):
    """执行代次已失效（任务被取消/回收/重试接管），写入必须丢弃。"""


async def user_lock(session: AsyncSession, user_id: uuid.UUID) -> None:
    """在当前事务内取同用户互斥锁；随本事务 commit/rollback 自动释放。

    调用方须保证本事务内不再嵌套会 commit 的外部调用。
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:uid))"),
        {"ns": LOCK_NAMESPACE, "uid": str(user_id)},
    )


@asynccontextmanager
async def critical(session: AsyncSession, user_id: uuid.UUID):
    """单事务临界区：取锁 → (yield 校验与写入) → commit；异常回滚。"""
    await user_lock(session, user_id)
    try:
        yield
        await session.commit()
    except BaseException:
        await session.rollback()
        raise


async def assert_writable(session: AsyncSession) -> None:
    """写入资格校验：无执行上下文（API 直写）时直接放行。

    有上下文（worker 产物落库）时必须行存在、仍在 RUNNING 且代次一致，
    否则丢弃结果——被取消/回收/重试接管的旧执行不得写库（PRD §7.1）。
    注意用 SELECT 强制落库：identity map 里的旧对象不反映其他连接的改动。
    """
    from sqlalchemy import select

    from app.models import ToolRun
    from app.models.tool_run import RunStatus

    context = current_execution.get()
    if context is None:
        return
    run_id, generation = context
    row = await session.execute(
        select(ToolRun.status, ToolRun.retries).where(ToolRun.id == run_id)
    )
    current = row.first()
    if current is None or current.status is not RunStatus.RUNNING or current.retries != generation:
        raise StaleExecution(f"run {run_id} 的执行代次已失效，结果被丢弃")
