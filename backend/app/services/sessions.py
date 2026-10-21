import hashlib
import uuid
from collections.abc import Iterable

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import security
from app.layers import BASE_LAYER_ID, LayerDocument, document_of
from app.models import Asset, EditHistory, EditSession, SessionAsset
from app.models.asset import SESSION_RETAINED_KINDS
from app.models.edit_history import HISTORY_LIMIT
from app.schemas.deletion import DeletionBlocker, SessionDeletionPreview
from app.services import guards, runs
from app.services.runs import ActiveTasks

TITLE_LIMIT = 80
DEFAULT_TITLE = "未命名会话"


class SessionNotFound(Exception):
    pass


class CannotUndo(Exception):
    pass


class CannotRedo(Exception):
    pass


class ReferenceStateInvalid(Exception):
    """历史/文档引用无法安全解析，必须保守阻止删除（PRD §7）。"""


class PreviewScopeChanged(Exception):
    """预览与实际删除之间的 C(X)/D(X) 已变化（LIB-FR-015）。"""


class ReferenceTargetGone(Exception):
    """引用写入时目标素材已被并发删除，写入被拒绝（PRD §7.1）。"""


def normalize_title(text: str | None) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[:TITLE_LIMIT] or DEFAULT_TITLE


def snapshot(record: EditSession) -> dict:
    return {
        "document": record.document,
        "current_asset_id": str(record.current_asset_id),
        "revision": record.revision,
    }


async def _next_position(session: AsyncSession, session_id: uuid.UUID) -> int:
    last = await session.scalar(
        select(func.max(SessionAsset.position)).where(SessionAsset.session_id == session_id)
    )
    return (last or 0) + 1


async def _attach(session: AsyncSession, record: EditSession, assets: Iterable[Asset]) -> int:
    """把资产挂进会话图片墙，返回新挂上的数量。"""
    known = set(
        await session.scalars(
            select(SessionAsset.asset_id).where(SessionAsset.session_id == record.id)
        )
    )
    position = await _next_position(session, record.id)
    added = 0

    for asset in assets:
        if asset.id in known:
            continue
        session.add(SessionAsset(session_id=record.id, asset_id=asset.id, position=position))
        known.add(asset.id)
        position += 1
        added += 1
    return added


async def _entry(session: AsyncSession, record: EditSession, seq: int) -> EditHistory | None:
    return await session.scalar(
        select(EditHistory).where(EditHistory.session_id == record.id, EditHistory.seq == seq)
    )


async def _max_seq(session: AsyncSession, record: EditSession) -> int:
    return (
        await session.scalar(
            select(func.max(EditHistory.seq)).where(EditHistory.session_id == record.id)
        )
        or 0
    )


async def _append_history(
    session: AsyncSession, record: EditSession, action: str, params: dict, result: dict
) -> int:
    seq = record.history_seq + 1
    session.add(
        EditHistory(
            user_id=record.user_id,
            session_id=record.id,
            seq=seq,
            action=action,
            params=params,
            result=result,
        )
    )
    await session.execute(
        delete(EditHistory).where(
            EditHistory.session_id == record.id, EditHistory.seq <= seq - HISTORY_LIMIT
        )
    )
    return seq


def _restore(record: EditSession, state: dict) -> None:
    record.document = state["document"]
    record.current_asset_id = uuid.UUID(state["current_asset_id"])
    record.revision = state["revision"]


async def apply_edit(
    session: AsyncSession,
    record: EditSession,
    action: str,
    *,
    params: dict | None = None,
    document: LayerDocument | None = None,
    current: Asset | None = None,
    extra_assets: Iterable[Asset] = (),
    result: dict | None = None,
    bump_revision: bool = True,
) -> EditSession:
    """应用一次可撤销编辑：先截断重做分支，再写入文档快照。

    引用写入的唯一汇聚点：与删除共用同用户锁，worker 迟到代次在此被丢弃；
    锁内重校验引用素材仍存在，避免「删除先提交→悬空引用」（PRD §7.1）。
    """
    await guards.user_lock(session, record.user_id)
    await guards.assert_writable(session)
    referenced = [current] if current is not None else []
    referenced += [asset for asset in extra_assets if asset is not None]
    if referenced:
        found = set(
            await session.scalars(select(Asset.id).where(Asset.id.in_([a.id for a in referenced])))
        )
        if found != {asset.id for asset in referenced}:
            raise ReferenceTargetGone("引用的素材已被删除")

    before = snapshot(record)
    next_current = record.current_asset_id
    next_document = record.document
    changed = False

    if current is not None and current.id != record.current_asset_id:
        next_current = current.id
        if document is None:
            document = document_of(current)
        changed = True
    if document is not None:
        payload = document.model_dump(mode="json")
        if payload != record.document:
            next_document = payload
            changed = True

    # 结果只进图片墙时画布没变，但仍要留一条记录，否则用户在编辑记录里找不到这次生成
    attached = await _attach(session, record, extra_assets)
    if not changed and not attached:
        await session.commit()
        await session.refresh(record)
        return record

    await session.execute(
        delete(EditHistory).where(
            EditHistory.session_id == record.id, EditHistory.seq > record.history_seq
        )
    )

    record.current_asset_id = next_current
    record.document = next_document
    if bump_revision:
        record.revision += 1

    record.history_seq = await _append_history(
        session,
        record,
        action,
        {**(params or {}), "before": before},
        {
            **(result or {}),
            "document": record.document,
            "current_asset_id": str(record.current_asset_id),
            "revision": record.revision,
        },
    )
    await session.commit()
    await session.refresh(record)
    return record


async def create(
    session: AsyncSession,
    user_id: uuid.UUID,
    current: Asset,
    wall: Iterable[Asset] = (),
    title: str | None = None,
) -> EditSession:
    """新建会话。current 进入画布，wall 中其余图片仅进图片墙备选。

    与删除同锁串行，锁内重校验素材仍存在（PRD §7.1）。
    """
    await guards.user_lock(session, user_id)
    wanted = {asset.id for asset in wall} | {current.id}
    found = set(await session.scalars(select(Asset.id).where(Asset.id.in_(wanted))))
    if found != wanted:
        raise ReferenceTargetGone("引用的素材已被删除")
    record = EditSession(
        user_id=user_id,
        title=normalize_title(title),
        original_asset_id=current.id,
        current_asset_id=current.id,
        document=document_of(current).model_dump(mode="json"),
        history_seq=0,
    )
    session.add(record)
    await session.flush()

    await _attach(session, record, [current, *wall])
    record.history_seq = await _append_history(
        session,
        record,
        "create_session",
        {},
        {"asset_id": str(current.id), **snapshot(record)},
    )
    await session.commit()
    await session.refresh(record)
    return record


async def load(session: AsyncSession, session_id: uuid.UUID) -> EditSession:
    """不带用户过滤的读取，仅供已确认归属的后台任务使用。"""
    record = await session.get(EditSession, session_id)
    if record is None:
        raise SessionNotFound
    return record


async def get_for_user(
    session: AsyncSession, session_id: uuid.UUID, user_id: uuid.UUID
) -> EditSession:
    record = await session.scalar(
        select(EditSession).where(EditSession.id == session_id, EditSession.user_id == user_id)
    )
    if record is None:
        raise SessionNotFound
    return record


async def list_for_user(
    session: AsyncSession, user_id: uuid.UUID, limit: int = 50
) -> list[EditSession]:
    result = await session.scalars(
        select(EditSession)
        .where(EditSession.user_id == user_id)
        .order_by(EditSession.updated_at.desc(), EditSession.id.asc())
        .limit(limit)
    )
    return list(result)


async def assets_of(session: AsyncSession, record: EditSession) -> list[Asset]:
    result = await session.scalars(
        select(Asset)
        .join(SessionAsset, SessionAsset.asset_id == Asset.id)
        .where(SessionAsset.session_id == record.id)
        .order_by(SessionAsset.position)
    )
    return list(result)


async def history_of(session: AsyncSession, record: EditSession) -> list[EditHistory]:
    result = await session.scalars(
        select(EditHistory)
        .where(EditHistory.session_id == record.id)
        .order_by(EditHistory.seq.desc())
    )
    return list(result)


async def undo_state(session: AsyncSession, record: EditSession) -> tuple[bool, bool]:
    return record.history_seq > 1, await _max_seq(session, record) > record.history_seq


async def previous_document(session: AsyncSession, record: EditSession) -> LayerDocument | None:
    """本轮操作前的画布，供前后对比。"""
    if record.history_seq <= 1:
        return None
    entry = await _entry(session, record, record.history_seq)
    before = (entry.params if entry else {}).get("before") or {}
    raw = before.get("document")
    return LayerDocument.model_validate(raw) if raw else None


async def record_result(
    session: AsyncSession,
    record: EditSession,
    assets: Iterable[Asset],
    action: str,
    params: dict,
    result: dict,
) -> EditSession:
    """工具产出并入图片墙。不改当前图时修订号保持不变。"""
    return await apply_edit(
        session,
        record,
        action,
        params=params,
        extra_assets=assets,
        result=result,
        bump_revision=False,
    )


async def rename(session: AsyncSession, record: EditSession, title: str) -> EditSession:
    record.title = normalize_title(title)
    await session.commit()
    await session.refresh(record)
    return record


async def switch_current(session: AsyncSession, record: EditSession, asset: Asset) -> EditSession:
    """切换画布当前图。修订号递增，使旧修订号上的选区与遮罩失效。"""
    document = await _last_document_for(session, record, asset.id)
    if _cropped_base(document, asset):
        document = document_of(asset)

    if record.current_asset_id == asset.id:
        # 裁剪不换资产，点自己等于要回到原图画幅；画布没被裁过则什么都不用做
        if not _cropped_base(LayerDocument.model_validate(record.document), asset):
            return record
        document = document_of(asset)

    return await apply_edit(
        session,
        record,
        "switch_current",
        current=asset,
        document=document,
        extra_assets=[asset],
    )


def _cropped_base(document: LayerDocument | None, asset: Asset) -> bool:
    """裁剪只缩小画布、不换资产。这类文档下点回这张图，应当是要它的原生画幅。"""
    if document is None or len(document.layers) != 1:
        return False
    layer = document.layers[0]
    if layer.id != BASE_LAYER_ID or layer.asset_id != asset.id:
        return False
    return document.width < asset.width or document.height < asset.height


def _document_for(state: dict | None, asset_id: str) -> LayerDocument | None:
    if not state or state.get("current_asset_id") != asset_id:
        return None
    raw = state.get("document")
    return LayerDocument.model_validate(raw) if raw else None


async def _last_document_for(
    session: AsyncSession, record: EditSession, asset_id: uuid.UUID
) -> LayerDocument | None:
    """该图上次在画布上的文档。没有则由 apply_edit 用 document_of 新建。"""
    target = str(asset_id)
    for entry in await history_of(session, record):
        found = _document_for(entry.result, target) or _document_for(
            (entry.params or {}).get("before"), target
        )
        if found is not None:
            return found
    return None


async def undo(session: AsyncSession, record: EditSession) -> EditSession:
    if record.history_seq <= 1:
        raise CannotUndo
    entry = await _entry(session, record, record.history_seq)
    before = (entry.params if entry else {}).get("before")
    if not before:
        raise CannotUndo
    _restore(record, before)
    record.history_seq -= 1
    await session.commit()
    await session.refresh(record)
    return record


async def redo(session: AsyncSession, record: EditSession) -> EditSession:
    entry = await _entry(session, record, record.history_seq + 1)
    if entry is None or "document" not in entry.result:
        raise CannotRedo
    _restore(record, entry.result)
    record.history_seq += 1
    await session.commit()
    await session.refresh(record)
    return record


# ---- 创意库 LIB：R(X)/C(X)/D(X) 全量引用扫描与会话删除（PRD §7、§8）----


def _as_uuid(raw: object) -> uuid.UUID:
    if not isinstance(raw, str):
        raise ReferenceStateInvalid("引用中的素材 ID 不是字符串")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ReferenceStateInvalid("引用中的素材 ID 无法解析") from exc


def _collect_state(state: object, type_: str, found: dict[uuid.UUID, set[str]]) -> None:
    """从一段 before/result 状态里收集 current_asset_id 与 document 图层引用。

    只认既有结构（current_asset_id / document.layers[].asset_id），
    不递归把任意 JSON 字符串猜作素材 ID（PRD §7）。
    """
    if not isinstance(state, dict):
        raise ReferenceStateInvalid("历史快照结构损坏")
    current = state.get("current_asset_id")
    if current is not None:
        found.setdefault(_as_uuid(current), set()).add(type_)
    document = state.get("document")
    if document is not None:
        try:
            layers = LayerDocument.model_validate(document).layers
        except ValidationError as exc:
            raise ReferenceStateInvalid("画布文档无法解析") from exc
        for layer in layers:
            if layer.asset_id is not None:
                found.setdefault(layer.asset_id, set()).add(type_)


async def reference_map(session: AsyncSession, record: EditSession) -> dict[uuid.UUID, set[str]]:
    """会话 X 的受保护引用集合 R(X)：asset_id -> 引用类型。

    覆盖 original/current、当前 document 全部图层（含隐藏图层）与全部保留历史
    （params.before 与 result，含重做分支）；损坏数据抛 ReferenceStateInvalid。
    """
    try:
        document_layers = LayerDocument.model_validate(record.document).layers
    except ValidationError as exc:
        raise ReferenceStateInvalid("画布文档无法解析") from exc

    found: dict[uuid.UUID, set[str]] = {
        record.original_asset_id: {"original"},
        record.current_asset_id: {"current"},
    }
    for layer in document_layers:
        if layer.asset_id is not None:
            found.setdefault(layer.asset_id, set()).add("layer")

    entries = await session.scalars(
        select(EditHistory).where(EditHistory.session_id == record.id)
    )
    for entry in entries:
        params = entry.params if isinstance(entry.params, dict) else None
        if params is None:
            raise ReferenceStateInvalid("编辑历史参数损坏")
        before = params.get("before")
        if before is not None:
            _collect_state(before, "history", found)
        result = entry.result if isinstance(entry.result, dict) else None
        if result is None:
            raise ReferenceStateInvalid("编辑历史结果损坏")
        _collect_state(result, "history", found)
    return found


async def references_of_user(
    session: AsyncSession, user_id: uuid.UUID, *, strict: bool = True
    ) -> dict[uuid.UUID, dict[uuid.UUID, tuple[str, set[str]]]]:
    """全量引用索引：asset_id -> {session_id: (title, types)}。

    供单删/批删的 R(X) 检查与素材库 leftover 全量归属判断；strict=False 时
    跳过无法解析的会话（列表展示不因脏数据失败，删除路径保持 strict）。
    """
    index: dict[uuid.UUID, dict[uuid.UUID, tuple[str, set[str]]]] = {}
    records = await session.scalars(select(EditSession).where(EditSession.user_id == user_id))
    for record in records:
        try:
            found = await reference_map(session, record)
        except ReferenceStateInvalid:
            if strict:
                raise
            continue
        for asset_id, types in found.items():
            entry = index.setdefault(asset_id, {})
            _, merged = entry.get(record.id, (record.title, set()))
            entry[record.id] = (record.title, merged | types)
    return index


async def deletion_scan(
    session: AsyncSession, record: EditSession
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """计算 (C(X), D(X))，全部基于数据库全量数据，不受列表 limit 影响。"""
    collection = set((await reference_map(session, record)).keys())
    collection |= set(
        await session.scalars(
            select(SessionAsset.asset_id).where(SessionAsset.session_id == record.id)
        )
    )

    # 其他会话的墙与 R(Y)：任何一处引用都使素材脱离 D(X)
    others_wall = set(
        await session.scalars(
            select(SessionAsset.asset_id)
            .join(EditSession, EditSession.id == SessionAsset.session_id)
            .where(EditSession.user_id == record.user_id, EditSession.id != record.id)
        )
    )
    for other in await session.scalars(
        select(EditSession).where(
            EditSession.user_id == record.user_id, EditSession.id != record.id
        )
    ):
        others_wall |= set((await reference_map(session, other)).keys())

    rows = (
        list(await session.scalars(select(Asset).where(Asset.id.in_(collection))))
        if collection
        else []
    )
    by_id = {asset.id: asset for asset in rows}
    deletable = {
        asset_id
        for asset_id in collection
        if asset_id in by_id
        and by_id[asset_id].user_id == record.user_id
        and by_id[asset_id].kind not in SESSION_RETAINED_KINDS
        and asset_id not in others_wall
    }
    return collection, deletable


def _digest(ids: set[uuid.UUID]) -> str:
    joined = ",".join(sorted(str(item) for item in ids))
    return hashlib.sha256(joined.encode()).hexdigest()


async def deletion_preview_for_user(
    session: AsyncSession, record: EditSession
) -> SessionDeletionPreview:
    """只读预览：活动任务门禁 + 全量 C/D 计数 + 签名确认凭证。"""
    blocked = await runs.has_active_tasks(session, record.user_id)
    collection, deletable = await deletion_scan(session, record)
    token, expires_at = security.issue_deletion_token(
        record.user_id, record.id, _digest(collection), _digest(deletable)
    )
    blockers = (
        [DeletionBlocker(code="active_tasks", message="有任务或计划未结束，请完成或取消后重试")]
        if blocked
        else []
    )
    return SessionDeletionPreview(
        session_id=record.id,
        delete_asset_count=len(deletable),
        retained_asset_count=len(collection - deletable),
        blocked=blocked,
        blockers=blockers,
        preview_token=token,
        expires_at=expires_at,
    )


async def delete_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    expect: dict | None = None,
) -> list[tuple[uuid.UUID, str]]:
    """会话删除临界区：锁内全量重算全部安全条件后删除（PRD FR-015）。

    会话删除、子表级联与 D(X) 资产删除在同一事务内完成；返回待清理的
    (asset_id, storage_key)，调用方在事务提交后执行对象清理（LIB-FR-017）。
    """
    pending: list[tuple[uuid.UUID, str]] = []
    async with guards.critical(session, user_id):
        record = await session.scalar(
            select(EditSession).where(EditSession.id == session_id, EditSession.user_id == user_id)
        )
        if record is None:
            raise SessionNotFound
        if await runs.has_active_tasks(session, user_id):
            raise ActiveTasks("有任务或计划未结束，请完成或取消后重试")
        collection, deletable = await deletion_scan(session, record)
        if expect is not None:
            if _digest(collection) != expect.get("c") or _digest(deletable) != expect.get("d"):
                raise PreviewScopeChanged
        if deletable:
            pending = [
                (asset.id, asset.storage_key)
                for asset in await session.scalars(select(Asset).where(Asset.id.in_(deletable)))
            ]
        # 显式保证会话删除/flush 在资产删除前（PRD §7）
        await session.delete(record)
        await session.flush()
        if deletable:
            await session.execute(delete(Asset).where(Asset.id.in_(deletable)))
    return pending
