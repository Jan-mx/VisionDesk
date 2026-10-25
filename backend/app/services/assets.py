import logging
import uuid
from collections import defaultdict

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.db import SessionFactory
from app.models import Asset, EditHistory, EditSession, PendingObjectWrite, SessionAsset, User
from app.models.asset import UNMANAGEABLE_KINDS, AssetKind, AssetSource
from app.models.pending_object_write import PendingWriteState
from app.models.user import UserStatus
from app.services import guards, runs
from app.services.images import ImageMeta, probe
from app.services.runs import ActiveTasks
from app.services.sessions import (
    ReferenceStateInvalid,
    _collect_state,
    reference_map,
    references_of_user,
)

# 遮罩、抠图层是会话内部产物，创作页历史素材不单独摊开（= UNMANAGEABLE_KINDS）
_WORKING = UNMANAGEABLE_KINDS
ORPHAN_TITLE = "未归入会话"

logger = logging.getLogger(__name__)


class AssetNotFound(Exception):
    pass


class UnsupportedAssetKind(Exception):
    pass


class AssetInUse(Exception):
    """素材仍被 original/current/图层引用，不能删除（LIB-FR-007，2026-10-23 修订）。"""

    def __init__(self, references: list[dict]):
        super().__init__("素材仍被会话引用")
        self.references = references


def _storage_key(user_id: uuid.UUID, asset_id: uuid.UUID, extension: str) -> str:
    return f"users/{user_id}/{asset_id}.{extension}"


async def _register_pending_write(
    user_id: uuid.UUID, storage_key: str, run_id: uuid.UUID | None, retries: int | None
) -> uuid.UUID:
    """写入屏障第一段（WI 决策 2a）：独立短事务检查并登记在途写入。

    用户缺失 → StaleExecution（worker 迟到执行语义，tools.execute 静默丢弃）；
    冻结（deleting）→ AccountDeleting；两种情形都不落登记行、不 PUT 对象。
    登记与调用方事务解耦：登记一旦提交即跨进程可见，崩溃残留由恢复扫描回收。
    """
    async with SessionFactory() as session:
        row = await session.execute(select(User.status).where(User.id == user_id))
        current = row.first()
        if current is None:
            raise guards.StaleExecution(f"用户 {user_id} 不存在，对象写入被丢弃")
        if current[0] is UserStatus.DELETING:
            raise guards.AccountDeleting(f"账号 {user_id} 删除中，对象写入被拒绝")
        pending = PendingObjectWrite(
            user_id=user_id, storage_key=storage_key, run_id=run_id, retries=retries
        )
        session.add(pending)
        await session.commit()
        return pending.id


async def _compensate_pending_write(pending_id: uuid.UUID, storage_key: str) -> None:
    """写入屏障补偿（WI 决策 2c，锁外执行）：删除已上传对象并置登记行 aborted。

    两步各自尽力而为：对象删除失败残留的孤儿与未终结的 registered 行
    均由恢复扫描兜底回收（tasks/deletion.py）。
    """
    try:
        await storage.delete(storage_key)
    except Exception:  # noqa: BLE001 — 补偿失败不掩盖原拒绝异常，恢复扫描兜底
        logger.warning("屏障补偿对象删除失败 storage_key=%s", storage_key, exc_info=True)
    try:
        async with SessionFactory() as session:
            await session.execute(
                update(PendingObjectWrite)
                .where(
                    PendingObjectWrite.id == pending_id,
                    PendingObjectWrite.state == PendingWriteState.REGISTERED,
                )
                .values(state=PendingWriteState.ABORTED)
            )
            await session.commit()
    except Exception:  # noqa: BLE001 — 恢复扫描兜底
        logger.warning("屏障补偿登记行置 aborted 失败 pending_id=%s", pending_id, exc_info=True)


async def _assert_user_writable(session: AsyncSession, user_id: uuid.UUID) -> None:
    """写入屏障锁内重读（WI 决策 2c）：临界区内校验用户仍可写。

    冻结（deleting）→ AccountDeleting；用户行已删 → StaleExecution。
    必须先于 assert_writable 执行：屏障测试在 assert_writable 的暂停点注入
    「校验后、提交前」冻结，重读放在其后会把迟到提交误判为拒绝。
    """
    row = await session.execute(select(User.status).where(User.id == user_id))
    current = row.first()
    if current is None:
        raise guards.StaleExecution(f"用户 {user_id} 已删除，写入被丢弃")
    if current[0] is UserStatus.DELETING:
        raise guards.AccountDeleting(f"账号 {user_id} 删除中，写入被拒绝")


async def create_from_bytes(
    session: AsyncSession,
    user_id: uuid.UUID,
    data: bytes,
    kind: AssetKind,
    source: AssetSource,
    meta: ImageMeta | None = None,
) -> Asset:
    """校验图片、登记在途写入、写入对象存储并落库（PRD §6.4 写入冻结屏障）。

    屏障时序（WI-20261025-03 决策 2）：独立短事务查用户状态并登记（deleting 拒绝，
    不落登记行不 PUT）→ storage.put（不持锁）→ 既有临界区内锁内重读用户状态 +
    assert_writable 执行代次校验，被拒（迟到代次或冻结）→ 锁外补偿（删对象 +
    置 aborted）；通过 → 资产行与登记行置 committed 同事务提交。S3 写入不持锁；
    落库事务与删除同锁串行，worker 迟到代次在此被丢弃。
    """
    meta = meta or probe(data)
    asset_id = uuid.uuid4()
    key = _storage_key(user_id, asset_id, meta.extension)

    execution = guards.current_execution.get()
    run_id, generation = execution if execution is not None else (None, None)
    pending_id = await _register_pending_write(user_id, key, run_id, generation)
    try:
        await storage.put(key, data, meta.content_type)
        try:
            async with guards.critical(session, user_id):
                await _assert_user_writable(session, user_id)
                await guards.assert_writable(session)
                asset = Asset(
                    id=asset_id,
                    user_id=user_id,
                    kind=kind,
                    source=source,
                    storage_key=key,
                    image_format=meta.image_format,
                    width=meta.width,
                    height=meta.height,
                    size_bytes=meta.size_bytes,
                    has_alpha=meta.has_alpha,
                )
                session.add(asset)
                # 登记行与资产行同事务置 committed（WI 决策 2c）
                await session.execute(
                    update(PendingObjectWrite)
                    .where(PendingObjectWrite.id == pending_id)
                    .values(state=PendingWriteState.COMMITTED)
                )
        except (guards.AccountDeleting, guards.StaleExecution):
            await _compensate_pending_write(pending_id, key)
            raise
    except Exception:
        # 其余异常（如资产行落库 DB 故障）：对象与 registered 登记行交由
        # 恢复扫描按超时回收（WI 决策 4d），此处不补偿以免误删重试语义。
        raise
    return asset


async def list_for_user(session: AsyncSession, user_id: uuid.UUID, limit: int = 50) -> list[Asset]:
    result = await session.scalars(
        select(Asset).where(Asset.user_id == user_id).order_by(Asset.created_at.desc()).limit(limit)
    )
    return list(result)


async def get_for_user(
    session: AsyncSession, user_id: uuid.UUID, asset_id: uuid.UUID
) -> Asset | None:
    """按主键与 user_id 联合查询，避免越权访问他人素材。"""
    return await session.scalar(select(Asset).where(Asset.id == asset_id, Asset.user_id == user_id))


async def rename(session: AsyncSession, asset: Asset, title: str | None) -> Asset:
    """重命名不受活动任务门禁与删除锁影响（PRD §7.1）。"""
    asset.title = title
    await session.commit()
    return asset


def _reference_rows(by_session: dict[uuid.UUID, tuple[str, set[str]]]) -> list[dict]:
    return [
        {"session_id": session_id, "title": title, "types": sorted(types)}
        for session_id, (title, types) in by_session.items()
    ]


# 2026-10-23 修订（LIB-FR-007）：history 不再阻断删除，409 references 只列阻断类型
_BLOCKING_TYPES = frozenset({"original", "current", "layer"})

_TYPE_LABELS = {"original": "原图", "current": "当前图", "layer": "图层"}


def _blocking_rows(
    references: dict[uuid.UUID, dict[uuid.UUID, tuple[str, set[str]]]], asset_id: uuid.UUID
) -> list[dict]:
    """从全量引用索引提取阻断会话与阻断类型（PRD §8.1，2026-10-23 修订）。"""
    blocking: dict[uuid.UUID, tuple[str, set[str]]] = {}
    for session_id, (title, types) in references.get(asset_id, {}).items():
        kept = types & _BLOCKING_TYPES
        if kept:
            blocking[session_id] = (title, kept)
    return _reference_rows(blocking)


def in_use_reason(references: list[dict]) -> str:
    """409 asset_in_use 的中文原因（用户端与管理端批删共用，WI-20261025-04 决策 5）。"""
    parts = []
    for reference in references:
        labels = "、".join(_TYPE_LABELS.get(kind, kind) for kind in reference["types"])
        parts.append(f"《{reference['title']}》的{labels}")
    return "素材仍被会话" + "；会话".join(parts) + "引用，请先删除对应会话"


# ---- 管理端素材引用扫描（WI-20261025-04 决策 2，与用户端 R(X) 同口径）----


async def scan_user_references(
    session: AsyncSession, user_id: uuid.UUID, *, strict: bool = True
) -> dict:
    """单遍扫描某用户全部会话的 R(X) 引用与历史文档计数。

    返回 {"assets": {asset_id: {"types": {session_id: set[str]}, "history_docs": int}},
    "titles": {session_id: title}}；types 与 reference_map 同口径
    （original/current/layer/history）；history_docs 统计 ``params.before`` 状态
    （撤销可回退画布）中引用该素材的历史文档数。strict=False 时跳过无法解析的
    会话（展示路径不因脏数据失败，删除路径保持 strict，与 references_of_user 一致）。
    """
    assets: dict[uuid.UUID, dict] = {}
    titles: dict[uuid.UUID, str] = {}
    records = list(
        await session.scalars(select(EditSession).where(EditSession.user_id == user_id))
    )
    for record in records:
        titles[record.id] = record.title
        try:
            found = await reference_map(session, record)
        except ReferenceStateInvalid:
            if strict:
                raise
            continue
        for asset_id, types in found.items():
            entry = assets.setdefault(asset_id, {"types": {}, "history_docs": 0})
            entry["types"][record.id] = set(types)
        # 历史文档计数：reference_map 已按同一结构校验过本会话全部条目
        # （params 必为 dict、result 必为 dict、before/result 状态可解析），
        # 此遍不会遇到新形态的脏数据；只统计 params.before——撤销可回退的
        # 画布快照（create 条目无 before 不计，与专项测试基构一致）。
        entries = list(
            await session.scalars(
                select(EditHistory).where(EditHistory.session_id == record.id)
            )
        )
        for hist in entries:
            before = (
                hist.params.get("before")
                if isinstance(hist.params, dict)
                else None
            )
            if before is None:
                continue
            scratch: dict[uuid.UUID, set[str]] = {}
            _collect_state(before, "history", scratch)
            for asset_id in scratch:
                assets.setdefault(asset_id, {"types": {}, "history_docs": 0})[
                    "history_docs"
                ] += 1
    return {"assets": assets, "titles": titles}


async def asset_references(
    session: AsyncSession, asset: Asset, *, strict: bool = False
) -> dict:
    """素材全量引用拓扑（WI-20261025-04 决策 2）：与用户端 R(X) 同一口径，
    全量扫描、不受任何页面过滤限制。

    - original_of / current_of：引用该素材为原图/当前图的会话 id 集合（str）；
    - layer_in：引用该素材的历史文档数（params.before 撤销可回退画布）；
    - wall_sessions：图片墙关联会话 id 集合（墙关联不构成删除保护，仅展示）；
    - active_tasks：同 owner 是否存在 queued/running 任务或待确认计划；
    - types_by_session / session_titles：str 键视图，供管理端详情引用行与
      计数摘要复用（id 以字符串呈现，会话拓扑对外的唯一形态）。
    """
    scan = await scan_user_references(session, asset.user_id, strict=strict)
    entry = scan["assets"].get(asset.id) or {"types": {}, "history_docs": 0}
    wall = set(
        await session.scalars(
            select(SessionAsset.session_id).where(SessionAsset.asset_id == asset.id)
        )
    )
    return {
        "original_of": {
            str(sid) for sid, types in entry["types"].items() if "original" in types
        },
        "current_of": {
            str(sid) for sid, types in entry["types"].items() if "current" in types
        },
        "layer_in": entry["history_docs"],
        "wall_sessions": {str(sid) for sid in wall},
        "active_tasks": await runs.has_active_tasks(session, asset.user_id),
        "types_by_session": {
            str(sid): set(types) for sid, types in entry["types"].items()
        },
        "session_titles": {str(sid): title for sid, title in scan["titles"].items()},
    }


async def _sweep_hidden(
    session: AsyncSession,
    user_id: uuid.UUID,
    references: dict[uuid.UUID, dict[uuid.UUID, tuple[str, set[str]]]],
) -> list[tuple[uuid.UUID, str]]:
    """物理回收已无任何 R(X)/墙引用的隐藏素材，返回 (asset_id, storage_key) 列表。

    调用方须已持有同用户临界区锁；references 为同事务内计算的全量引用索引
    （LIB-FR-019，PRD §7 延迟物理回收）。
    """
    hidden = list(
        await session.scalars(
            select(Asset).where(Asset.user_id == user_id, Asset.library_hidden.is_(True))
        )
    )
    if not hidden:
        return []
    # 防御：正常不变量下隐藏素材无墙关联，仍以墙数据兜底排除
    on_wall = set(
        await session.scalars(
            select(SessionAsset.asset_id)
            .join(EditSession, EditSession.id == SessionAsset.session_id)
            .where(EditSession.user_id == user_id)
        )
    )
    reclaimed: list[tuple[uuid.UUID, str]] = []
    for asset in hidden:
        if asset.id in references or asset.id in on_wall:
            continue
        reclaimed.append((asset.id, asset.storage_key))
        await session.delete(asset)
    return reclaimed


async def sweep_hidden_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    """独立扫尾临界区（批删收尾等场景）：引用损坏时保守跳过并告警（LIB-FR-019）。"""
    async with guards.critical(session, user_id):
        try:
            references = await references_of_user(session, user_id)
        except ReferenceStateInvalid:
            logger.warning("隐藏素材扫尾跳过：引用数据无法安全解析 user_id=%s", user_id)
            return []
        return await _sweep_hidden(session, user_id, references)


async def delete_for_user(
    session: AsyncSession, user_id: uuid.UUID, asset_id: uuid.UUID
) -> list[tuple[uuid.UUID, str]]:
    """单删临界区：锁内重算 kind/引用/活动任务后物理或隐藏式删除。

    返回待清理对象 (asset_id, storage_key) 列表（含延迟回收扫尾），由调用方在
    提交后清理；仅历史引用走隐藏式删除，不产生对象清理（LIB-FR-007/018/019）。
    """
    async with guards.critical(session, user_id):
        asset = await session.scalar(
            select(Asset).where(Asset.id == asset_id, Asset.user_id == user_id)
        )
        if asset is None or asset.library_hidden:
            # 已隐藏素材不再暴露管理入口：单删 404 / 批删 not_found（LIB-FR-011）
            raise AssetNotFound
        if asset.kind in UNMANAGEABLE_KINDS:
            raise UnsupportedAssetKind
        if await runs.has_active_tasks(session, user_id):
            raise ActiveTasks("有任务或计划未结束，请完成或取消后重试")
        references = await references_of_user(session, user_id)
        blocking = _blocking_rows(references, asset.id)
        if blocking:
            raise AssetInUse(blocking)
        reclaimed: list[tuple[uuid.UUID, str]] = []
        if asset.id in references:
            # 仅历史引用 → 隐藏式删除：行与对象保留，墙关联清空（LIB-FR-018）
            asset.library_hidden = True
            await session.execute(
                delete(SessionAsset).where(SessionAsset.asset_id == asset.id)
            )
        else:
            reclaimed.append((asset.id, asset.storage_key))
            # 墙关联由 FK CASCADE 级联移除
            await session.delete(asset)
        reclaimed.extend(await _sweep_hidden(session, user_id, references))
    return reclaimed


async def batch_delete_for_user(
    session: AsyncSession, user_id: uuid.UUID, asset_ids: list[uuid.UUID]
) -> tuple[list[uuid.UUID], list[dict]]:
    """批删（PRD §8.2）：请求级校验整批通过后逐项独立事务提交。"""
    # 请求级临界区：全量归属校验先于活动任务检查，任一失败零写入
    async with guards.critical(session, user_id):
        for asset_id in asset_ids:
            if await get_for_user(session, user_id, asset_id) is None:
                raise AssetNotFound
        if await runs.has_active_tasks(session, user_id):
            raise ActiveTasks("有任务或计划未结束，请完成或取消后重试")

    deleted: list[uuid.UUID] = []
    skipped: list[dict] = []
    for asset_id in asset_ids:
        try:
            reclaimed = await delete_for_user(session, user_id, asset_id)
        except AssetNotFound:
            skipped.append({"id": asset_id, "code": "not_found", "reason": "记录已不存在"})
            continue
        except UnsupportedAssetKind:
            skipped.append(
                {
                    "id": asset_id,
                    "code": "unsupported_asset_kind",
                    "reason": "会话内部派生素材，本期不支持删除",
                }
            )
            continue
        except ActiveTasks:
            skipped.append(
                {
                    "id": asset_id,
                    "code": "active_tasks",
                    "reason": "有任务或计划未结束，请完成或取消后重试",
                }
            )
            continue
        except AssetInUse as exc:
            skipped.append(
                {
                    "id": asset_id,
                    "code": "asset_in_use",
                    "reason": in_use_reason(exc.references),
                }
            )
            continue
        except ReferenceStateInvalid:
            skipped.append(
                {
                    "id": asset_id,
                    "code": "reference_state_invalid",
                    "reason": "引用数据无法安全解析，需修复后才能删除",
                }
            )
            continue
        deleted.append(asset_id)
        # 每项提交后即清理对象；失败仅告警，不把已提交项改为 skipped（LIB-FR-017）
        for swept_id, key in reclaimed:
            await cleanup_object(key, swept_id)
    # 批删收尾扫尾一次：覆盖全部逐项被跳过、无删除事务触发的场景（LIB-FR-019）
    for swept_id, key in await sweep_hidden_for_user(session, user_id):
        await cleanup_object(key, swept_id)
    return deleted, skipped


async def cleanup_object(key: str, asset_id: uuid.UUID) -> None:
    """DB 提交后的对象清理；失败记录可定位 warning，不回滚业务删除。"""
    try:
        await storage.delete(key)
    except Exception as exc:
        logger.warning(
            "对象清理失败 asset_id=%s storage_key=%s error=%s",
            asset_id,
            key,
            type(exc).__name__,
        )


def _visible(wall: list[Asset], current_asset_id: uuid.UUID | None) -> list[Asset]:
    """会话组可见素材，与编辑器图片墙（ImageWall）同语义：

    当前作品（current_asset_id）+ wall 末位最新一张快照 + 全部常规素材；
    mask/subject/background 与旧快照继续过滤，leftover 不经过本函数（SQL 侧全量排除）。
    隐藏素材不展示，但当前作品锚点例外——撤销可把已隐藏素材恢复为当前图（LIB-FR-018）。
    """
    latest_snapshot_id = next(
        (asset.id for asset in reversed(wall) if asset.kind == AssetKind.SNAPSHOT), None
    )
    return [
        asset
        for asset in wall
        if asset.id == current_asset_id
        or (
            not asset.library_hidden
            and (asset.id == latest_snapshot_id or asset.kind not in _WORKING)
        )
    ]


async def library_for_user(
    session: AsyncSession, user_id: uuid.UUID, limit: int = 50
) -> list[tuple[EditSession | None, Asset, list[Asset]]]:
    """创作页素材：按会话收拢，未进过会话的生成/上传单独一组。

    leftover 按全量归属判断：本人任何会话的墙或 R(X) 都不算未归组，
    先完成全量排除再按 created_at 倒序截断 limit（PRD §7）。
    """
    records = list(
        await session.scalars(
            select(EditSession)
            .where(EditSession.user_id == user_id)
            .order_by(EditSession.updated_at.desc(), EditSession.id.asc())
            .limit(limit)
        )
    )
    walls: dict[uuid.UUID, list[Asset]] = defaultdict(list)
    if records:
        rows = await session.execute(
            select(SessionAsset.session_id, Asset)
            .join(Asset, Asset.id == SessionAsset.asset_id)
            .where(SessionAsset.session_id.in_([record.id for record in records]))
            .order_by(SessionAsset.position)
        )
        for session_id, asset in rows:
            walls[session_id].append(asset)

    cover_ids = {record.current_asset_id for record in records}
    covers = {
        asset.id: asset
        for asset in await session.scalars(select(Asset).where(Asset.id.in_(cover_ids)))
    } if cover_ids else {}

    groups: list[tuple[EditSession | None, Asset, list[Asset]]] = []
    for record in records:
        visible = _visible(walls.get(record.id, []), record.current_asset_id)
        cover = covers.get(record.current_asset_id)
        if cover is None and visible:
            cover = visible[0]
        if cover is None:
            continue
        if cover.id not in {asset.id for asset in visible}:
            # 撤销/重做把已隐藏素材恢复为当前图时墙关联已删，但当前作品锚点
            # 必须随会话组展示，与编辑器图片墙计数对齐（LIB-FR-018，2026-10-23）
            visible = [*visible, cover]
        groups.append((record, cover, visible or [cover]))

    # 全量归属：所有会话的墙 + 所有会话的 R(X)（损坏会话跳过，不影响列表展示）
    attached = set(
        await session.scalars(
            select(SessionAsset.asset_id)
            .join(EditSession, EditSession.id == SessionAsset.session_id)
            .where(EditSession.user_id == user_id)
        )
    )
    attached |= set((await references_of_user(session, user_id, strict=False)).keys())

    leftover = list(
        await session.scalars(
            select(Asset)
            .where(
                Asset.user_id == user_id,
                Asset.kind.notin_(_WORKING),
                Asset.library_hidden.is_(False),
                *([Asset.id.notin_(attached)] if attached else []),
            )
            .order_by(Asset.created_at.desc())
            .limit(limit)
        )
    )
    if leftover:
        groups.append((None, leftover[0], leftover))
    return groups


# 会话墙上可批量处理的常规素材种类；排除内部遮罩、拆层件与画布快照
_PROCESSABLE_KINDS = {"original", "generated", "marketing", "export"}


async def processable_wall_assets(session: AsyncSession, record: EditSession) -> list[Asset]:
    """会话当前可见、可交给批量处理的常规素材（按墙序去重）。

    聊天批量目标与执行期兜底校验共用的会话域合法集合（AER-AC-023）。
    """
    from app.services import sessions as session_service

    wall = await session_service.assets_of(session, record)
    seen: set = set()
    result = []
    for asset in wall:
        if asset.library_hidden or asset.kind not in (
            AssetKind.ORIGINAL,
            AssetKind.GENERATED,
            AssetKind.MARKETING,
            AssetKind.EXPORT,
        ):
            continue
        if asset.id in seen:
            continue
        seen.add(asset.id)
        result.append(asset)
    return result
