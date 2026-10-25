"""账号清理任务与恢复扫描（WI-20261025-03 实现要求 4/5/6，PRD §6.3/§11）。

阶段机（operation.phase）：frozen → draining → business_rows → objects → finalize。

- 每阶段独立短事务推进，S3 网络调用一律在数据库事务之外（WI 红线）；
- business_rows / finalize 在目标用户锁内执行，与确认流程、写入屏障同锁串行；
- 任一阶段异常 → 操作置 failed + 脱敏 failure_code/detail，phase 保持断点，
  retry 与 cron 同 operation_id 续跑（business_rows 幂等重放、objects 按 key 幂等）；
- 恢复扫描 recover_deletions：重投未终结操作 + 回收崩溃残留登记行，并就地推进
  一次清理（投递副作用不可观测处的可执行契约：恢复后清理可续跑、同 op 幂等）；
- 同一目标禁止两个执行者并发清理：ARQ job_id 唯一 + processing 前置检查 +
  操作级 pg_try_advisory_lock 三重防护（WI 决策 4f）。
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.db import SessionFactory
from app.models import (
    AccountDeletionOperation,
    AdminAuditLog,
    AgentRun,
    Asset,
    EditHistory,
    EditSession,
    PendingObjectWrite,
    SessionAsset,
    ToolRun,
    User,
)
from app.models.account_deletion_operation import (
    PHASE_BUSINESS_ROWS,
    PHASE_DRAINING,
    PHASE_FINALIZE,
    PHASE_FROZEN,
    PHASE_OBJECTS,
    AccountDeletionStatus,
)
from app.models.pending_object_write import PendingWriteState
from app.services import account_deletion, audit, guards
from app.services.account_deletion import user_prefix

logger = logging.getLogger(__name__)

TASK = "cleanup_account"

# 崩溃残留判定阈值：登记→PUT→临界区在秒级完成；超时 registered 行视为进程崩溃
# 残留（远大于任何在途写窗口，远小于测试注入的 2h 旧数据）
RESIDUE_TIMEOUT_SECONDS = 600

# 每批删除的对象数上限（S3 delete_objects 单请求上限 1000）
_DELETE_BATCH = 1000

# 单次调用推进的阶段步上限；未终态由 cron/重试续跑
_MAX_STEPS_PER_RUN = 8


class CleanupError(Exception):
    """带稳定脱敏失败码的清理异常；detail 只保留异常类型名，不含对象 key/DSN。"""

    def __init__(self, code: str, cause: Exception):
        super().__init__(code)
        self.code = code
        self.cause = cause


async def _load_op(operation_id: uuid.UUID) -> AccountDeletionOperation | None:
    async with SessionFactory() as session:
        return await session.get(AccountDeletionOperation, operation_id)


async def _set_phase(operation_id: uuid.UUID, phase: str) -> None:
    async with SessionFactory() as session:
        await session.execute(
            update(AccountDeletionOperation)
            .where(AccountDeletionOperation.id == operation_id)
            .values(phase=phase)
        )
        await session.commit()


async def _set_phase_in(session: AsyncSession, operation_id: uuid.UUID, phase: str) -> None:
    """在当前事务内推进阶段（与业务写入同事务原子提交）。"""
    await session.execute(
        update(AccountDeletionOperation)
        .where(AccountDeletionOperation.id == operation_id)
        .values(phase=phase)
    )


async def _set_progress(operation_id: uuid.UUID, *, done: int, total: int) -> None:
    async with SessionFactory() as session:
        await session.execute(
            update(AccountDeletionOperation)
            .where(AccountDeletionOperation.id == operation_id)
            .values(progress_done=done, progress_total=total)
        )
        await session.commit()


async def _has_registered_writes(user_id: uuid.UUID) -> bool:
    async with SessionFactory() as session:
        pending = await session.scalar(
            select(func.count())
            .select_from(PendingObjectWrite)
            .where(
                PendingObjectWrite.user_id == user_id,
                PendingObjectWrite.state == PendingWriteState.REGISTERED,
            )
        )
    return pending > 0


async def _business_rows_empty(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """在线业务行数全零核对（含墙关联；finalize 双重核对之一，WI 决策 4e）。"""
    wall = await session.scalar(
        select(func.count())
        .select_from(SessionAsset)
        .join(EditSession, EditSession.id == SessionAsset.session_id)
        .where(EditSession.user_id == user_id)
    )
    if wall:
        return False
    for model in (EditSession, EditHistory, AgentRun, ToolRun, Asset):
        if await session.scalar(
            select(func.count()).select_from(model).where(model.user_id == user_id)
        ):
            return False
    return True


async def _purge_business_rows(operation_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """business_rows 阶段（WI 决策 4c）：目标用户锁内单事务清除全部在线业务行。

    顺序：墙 → 历史 → 两类任务 → 会话 → 素材。会话先于素材删除，edit_sessions
    对 assets 的 RESTRICT 引用随会话行消失（JSON 引用随会话行删除），无需置空
    引用列；任一删除触发 RESTRICT 即整体回滚并转 failed，不伪报部分完成。
    """
    async with SessionFactory() as session:
        async with guards.critical(session, user_id):
            exists = await session.scalar(select(User.id).where(User.id == user_id))
            if exists is None:
                # 墓碑已不存在：users 删除已级联全部业务行，直接推进
                await _set_phase_in(session, operation_id, PHASE_OBJECTS)
                return
            await session.execute(
                delete(SessionAsset).where(
                    SessionAsset.session_id.in_(
                        select(EditSession.id).where(EditSession.user_id == user_id)
                    )
                )
            )
            await session.execute(delete(EditHistory).where(EditHistory.user_id == user_id))
            await session.execute(delete(AgentRun).where(AgentRun.user_id == user_id))
            await session.execute(delete(ToolRun).where(ToolRun.user_id == user_id))
            await session.execute(delete(EditSession).where(EditSession.user_id == user_id))
            await session.execute(delete(Asset).where(Asset.user_id == user_id))
            await _set_phase_in(session, operation_id, PHASE_OBJECTS)


async def _prefix_items(user_id: uuid.UUID) -> list[tuple[str, str | None]]:
    """列举目标前缀下全部待删项 (Key, VersionId)：优先版本列举（连删除标记），
    失败回退 list_objects_v2（WI 决策 4d）。"""
    prefix = user_prefix(user_id)
    try:
        versions, markers = await storage.list_object_versions(prefix)
        return [*versions, *markers]
    except Exception:
        keys = await storage.list_keys(prefix)
        return [(key, None) for key in keys]


async def _prefix_item_count(user_id: uuid.UUID) -> int:
    prefix = user_prefix(user_id)
    try:
        versions, markers = await storage.list_object_versions(prefix)
        return len(versions) + len(markers)
    except Exception:
        return len(await storage.list_keys(prefix))


async def _purge_objects(operation_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """objects 阶段（WI 决策 4d，锁外）：按前缀分页列举删除，进度随批持久化。

    对象不存在视为已处理（S3 删除幂等）；列举/删除异常转 CleanupError 交由
    上层置 failed，绝不误报前缀为空。
    """
    items = await _prefix_items(user_id)
    total = len(items)
    await _set_progress(operation_id, done=0, total=total)
    done = 0
    for start in range(0, total, _DELETE_BATCH):
        chunk = items[start : start + _DELETE_BATCH]
        try:
            await storage.delete_objects(chunk)
        except Exception as exc:
            raise CleanupError("s3_delete_failed", exc) from exc
        done = min(start + _DELETE_BATCH, total)
        await _set_progress(operation_id, done=done, total=total)
    await _set_phase(operation_id, PHASE_FINALIZE)


async def _finalize(operation_id: uuid.UUID) -> bool:
    """finalize 阶段（WI 决策 4e）：三重核对通过才删墓碑并标记 succeeded。

    1. 前缀再扫为空（锁外）；2. 无 registered 登记行；3. 业务行数全零（锁内）。
    任一不过 → 阶段回退重扫，保持 processing，不伪报完成。通过时删除目标旧
    审计行（保留本操作链）、删除用户墓碑；user_delete 结果行已在确认时写入
    并在此保留（审计最小链仅此一行，summary 仅状态、无用户名/对象清单）。
    """
    async with SessionFactory() as session:
        op = await session.get(AccountDeletionOperation, operation_id)
        if op is None or op.status != AccountDeletionStatus.PROCESSING:
            return True
        user_id = op.target_user_id
    try:
        remaining = await _prefix_item_count(user_id)
    except Exception as exc:
        raise CleanupError("s3_list_failed", exc) from exc
    if remaining:
        await _set_phase(operation_id, PHASE_OBJECTS)
        return False

    ok = True
    async with SessionFactory() as session:
        async with guards.critical(session, user_id):
            registered = await session.scalar(
                select(func.count())
                .select_from(PendingObjectWrite)
                .where(
                    PendingObjectWrite.user_id == user_id,
                    PendingObjectWrite.state == PendingWriteState.REGISTERED,
                )
            )
            if registered:
                await _set_phase_in(session, operation_id, PHASE_DRAINING)
                ok = False
            elif not await _business_rows_empty(session, user_id):
                await _set_phase_in(session, operation_id, PHASE_BUSINESS_ROWS)
                ok = False
            else:
                # 删除目标旧审计行：本操作链（operation_id=本操作）之外的记录全部清除
                await session.execute(
                    delete(AdminAuditLog).where(
                        AdminAuditLog.target_type == audit.TARGET_USER,
                        AdminAuditLog.target_id == user_id,
                        or_(
                            AdminAuditLog.operation_id.is_(None),
                            AdminAuditLog.operation_id != operation_id,
                        ),
                    )
                )
                await session.execute(delete(User).where(User.id == user_id))
                await session.execute(
                    update(AccountDeletionOperation)
                    .where(AccountDeletionOperation.id == operation_id)
                    .values(
                        status=AccountDeletionStatus.SUCCEEDED,
                        finished_at=datetime.now(UTC),
                        failure_code=None,
                        failure_detail=None,
                    )
                )
                # 审计最小行 = 确认时写入并在此保留的 user_delete 结果行（无新增）
    return ok


async def _mark_failed(operation_id: uuid.UUID, code: str, cause: Exception) -> None:
    """置 failed + 脱敏失败码（仅 code 与异常类型名），phase 保持断点（WI 决策 4f）。"""
    logger.warning(
        "账号清理失败 operation_id=%s code=%s error=%s",
        operation_id,
        code,
        type(cause).__name__,
        exc_info=cause,
    )
    async with SessionFactory() as session:
        op = await session.get(AccountDeletionOperation, operation_id)
        if op is None or op.status != AccountDeletionStatus.PROCESSING:
            return
        await session.execute(
            update(AccountDeletionOperation)
            .where(
                AccountDeletionOperation.id == operation_id,
                AccountDeletionOperation.status == AccountDeletionStatus.PROCESSING,
            )
            .values(
                status=AccountDeletionStatus.FAILED,
                failure_code=code[:64],
                failure_detail=type(cause).__name__[:500],
            )
        )
        await session.commit()
        actor_id = op.actor_id
        target_id = op.target_user_id
    # 失败补记（WI 决策 6）：不关联 operation_id，finalize 清除旧记录时一并回收
    await audit.record_failed(
        actor_id=actor_id,
        target_type=audit.TARGET_USER,
        target_id=target_id,
        action=audit.USER_DELETE,
        request_id=str(uuid.uuid4()),
        summary={"status": "failed"},
    )


async def cleanup_account(ctx: dict, operation_id: uuid.UUID) -> None:
    """清理任务入口（ARQ 注册名 cleanup_account，WI 决策 4）。

    - 操作级 pg_try_advisory_lock：同一目标禁止两个执行者并发清理（决策 4f）；
    - 阶段机幂等断点续跑；任何异常 → 操作置 failed + 脱敏失败码，不向调用方抛出。
    """
    async with SessionFactory() as lock_session:
        acquired = (
            await lock_session.execute(
                text("SELECT pg_try_advisory_lock(:ns, hashtext(:key))"),
                {"ns": guards.LOCK_NAMESPACE, "key": f"deletion-op:{operation_id}"},
            )
        ).scalar_one()
        if not acquired:
            logger.info("清理操作已有执行者，跳过 operation_id=%s", operation_id)
            return
        try:
            await _run_phases(operation_id)
        except CleanupError as exc:
            await _mark_failed(operation_id, exc.code, exc.cause)
        except Exception as exc:  # noqa: BLE001 — 未预期故障同样落 failed，不向队列抛出
            await _mark_failed(operation_id, "cleanup_failed", exc)
        finally:
            try:
                await lock_session.execute(
                    text("SELECT pg_advisory_unlock(:ns, hashtext(:key))"),
                    {"ns": guards.LOCK_NAMESPACE, "key": f"deletion-op:{operation_id}"},
                )
            except Exception:  # noqa: BLE001 — 连接级解锁尽力而为，关闭连接兜底释放
                logger.warning("清理锁解锁失败 operation_id=%s", operation_id, exc_info=True)


async def _run_phases(operation_id: uuid.UUID) -> None:
    for _ in range(_MAX_STEPS_PER_RUN):
        op = await _load_op(operation_id)
        if op is None or op.status != AccountDeletionStatus.PROCESSING:
            return  # 幂等：非 processing 一律不执行（ARQ job_id 去重双保险）
        phase = op.phase
        if phase == PHASE_FROZEN:
            await _set_phase(operation_id, PHASE_DRAINING)
            continue
        if phase == PHASE_DRAINING:
            if await _has_registered_writes(op.target_user_id):
                return  # 在途写未了结：保持 processing，等待写入方自了结/恢复扫描
            await _set_phase(operation_id, PHASE_BUSINESS_ROWS)
            continue
        if phase == PHASE_BUSINESS_ROWS:
            try:
                await _purge_business_rows(operation_id, op.target_user_id)
            except CleanupError:
                raise
            except Exception as exc:
                raise CleanupError("business_rows_failed", exc) from exc
            continue
        if phase == PHASE_OBJECTS:
            try:
                await recycle_residue(op.target_user_id)
                await _purge_objects(operation_id, op.target_user_id)
            except CleanupError:
                raise
            except Exception as exc:
                raise CleanupError("objects_failed", exc) from exc
            continue
        if phase == PHASE_FINALIZE:
            if await _finalize(operation_id):
                return
            continue
        raise CleanupError("unknown_phase", RuntimeError(f"unexpected phase {phase!r}"))


async def _set_write_state(write_id: uuid.UUID, state: PendingWriteState) -> None:
    async with SessionFactory() as session:
        await session.execute(
            update(PendingObjectWrite)
            .where(
                PendingObjectWrite.id == write_id,
                PendingObjectWrite.state == PendingWriteState.REGISTERED,
            )
            .values(state=state)
        )
        await session.commit()


async def recycle_residue(user_id: uuid.UUID | None = None) -> int:
    """回收崩溃残留登记行（WI 决策 4d）：registered 且超时——

    有对应资产行说明已提交漏标记 → 置 committed；否则补偿删对象 + 置 aborted。
    对象删除失败时保留 registered 行，由下一轮扫描重试。S3 调用在事务外。
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=RESIDUE_TIMEOUT_SECONDS)
    async with SessionFactory() as session:
        stmt = select(PendingObjectWrite).where(
            PendingObjectWrite.state == PendingWriteState.REGISTERED,
            PendingObjectWrite.created_at < cutoff,
        )
        if user_id is not None:
            stmt = stmt.where(PendingObjectWrite.user_id == user_id)
        rows = list(await session.scalars(stmt))

    recycled = 0
    for row in rows:
        async with SessionFactory() as session:
            has_asset = await session.scalar(
                select(Asset.id).where(
                    Asset.user_id == row.user_id, Asset.storage_key == row.storage_key
                )
            )
        if has_asset is not None:
            await _set_write_state(row.id, PendingWriteState.COMMITTED)
            recycled += 1
            continue
        try:
            await storage.delete(row.storage_key)
        except Exception:  # noqa: BLE001 — 删除失败保留 registered，下轮重试
            logger.warning(
                "残留登记行对象补偿失败 storage_key=%s", row.storage_key, exc_info=True
            )
            continue
        await _set_write_state(row.id, PendingWriteState.ABORTED)
        recycled += 1
    return recycled


async def recover_deletions(ctx: dict) -> int:
    """恢复扫描（WI 决策 5，每分钟 cron）：重投未终结操作 + 回收残留登记行，
    并就地推进一次清理——投递丢失/无消费者的操作在扫描内续跑至终态或断点；
    同 operation_id 幂等，重复执行不产生重复审计或重复删除。"""
    async with SessionFactory() as session:
        operation_ids = list(
            await session.scalars(
                select(AccountDeletionOperation.id).where(
                    AccountDeletionOperation.status.in_(
                        [AccountDeletionStatus.PROCESSING, AccountDeletionStatus.FAILED]
                    )
                )
            )
        )

    for operation_id in operation_ids:
        try:
            from app.queue import enqueue

            await enqueue(TASK, operation_id)
        except Exception:  # noqa: BLE001 — 投递失败不阻断就地推进
            logger.warning("恢复扫描投递失败 operation_id=%s", operation_id, exc_info=True)

    await recycle_residue()
    for operation_id in operation_ids:
        # 就地推进（可观测契约）：cleanup_account 自带幂等与执行者互斥
        await cleanup_account({}, operation_id)
    return len(operation_ids)
