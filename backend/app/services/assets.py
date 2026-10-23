import logging
import uuid
from collections import defaultdict

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.models import Asset, EditSession, SessionAsset
from app.models.asset import UNMANAGEABLE_KINDS, AssetKind, AssetSource
from app.services import guards, runs
from app.services.images import ImageMeta, probe
from app.services.runs import ActiveTasks
from app.services.sessions import ReferenceStateInvalid, references_of_user

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


async def create_from_bytes(
    session: AsyncSession,
    user_id: uuid.UUID,
    data: bytes,
    kind: AssetKind,
    source: AssetSource,
    meta: ImageMeta | None = None,
) -> Asset:
    """校验图片、写入对象存储并落库。所有素材以 user_id 为前缀隔离。

    S3 写入不持锁；落库事务与删除同锁串行，worker 迟到代次在此被丢弃。
    """
    meta = meta or probe(data)
    asset_id = uuid.uuid4()
    key = _storage_key(user_id, asset_id, meta.extension)

    await storage.put(key, data, meta.content_type)

    await guards.user_lock(session, user_id)
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
    await session.commit()
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


def _in_use_reason(references: list[dict]) -> str:
    parts = []
    for reference in references:
        labels = "、".join(_TYPE_LABELS.get(kind, kind) for kind in reference["types"])
        parts.append(f"《{reference['title']}》的{labels}")
    return "素材仍被会话" + "；会话".join(parts) + "引用，请先删除对应会话"


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
                    "reason": _in_use_reason(exc.references),
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
