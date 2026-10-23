import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionDep
from app.deps import CurrentUser
from app.models import Asset, EditSession, User
from app.schemas.agent import MessageIn, TurnOut
from app.schemas.asset import AssetOut
from app.schemas.deletion import DeletionErrorDetail, SessionDeletionPreview
from app.schemas.export import ExportIn
from app.schemas.run import RunOut
from app.schemas.session import (
    HistoryOut,
    SelectIn,
    SelectionOut,
    SessionCreateIn,
    SessionDetailOut,
    SessionOut,
    SessionPatchIn,
    ToolInvokeIn,
    ToolInvokeOut,
)
from app.security import PreviewTokenError, PreviewTokenExpired, read_deletion_token
from app.services import agent as agent_service
from app.services import assets as asset_service
from app.services import exports, selections, sessions, tools
from app.services.agent import CannotCancel, CannotConfirm, CannotRetry, TurnNotFound
from app.services.exports import UnknownExportAsset
from app.services.runs import ActiveTasks
from app.services.selections import EmptySelection, StaleSelection
from app.services.sessions import (
    CannotRedo,
    CannotUndo,
    PreviewScopeChanged,
    ReferenceStateInvalid,
    ReferenceTargetGone,
    SessionNotFound,
)
from app.services.tools import InvalidParams
from app.tools import UnknownTool

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _deletion_error(
    status_code: int, code: str, message: str, references: list | None = None
) -> HTTPException:
    detail = DeletionErrorDetail(code=code, message=message, references=references)
    return HTTPException(status_code, detail=detail.model_dump(mode="json", exclude_none=True))


async def _asset(session: AsyncSession, user: User, asset_id: uuid.UUID) -> Asset:
    asset = await asset_service.get_for_user(session, user.id, asset_id)
    # 已隐藏素材不可作为新建会话的起点（LIB-FR-011/018，2026-10-23）
    if asset is None or asset.library_hidden:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "素材不存在")
    return asset


async def _load(session: AsyncSession, user: User, session_id: uuid.UUID) -> EditSession:
    try:
        return await sessions.get_for_user(session, session_id, user.id)
    except SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在") from exc


async def _detail(session: AsyncSession, record: EditSession) -> SessionDetailOut:
    # 墙 ∪ 活跃状态素材：撤销/重做把已隐藏素材恢复为当前图/图层时仍可渲染（LIB-FR-018）
    wall = await sessions.live_assets_of(session, record)
    can_undo, can_redo = await sessions.undo_state(session, record)
    return SessionDetailOut.of_detail(
        record,
        [AssetOut.of(asset) for asset in wall],
        previous_document=await sessions.previous_document(session, record),
        can_undo=can_undo,
        can_redo=can_redo,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreateIn, user: CurrentUser, session: SessionDep
) -> SessionDetailOut:
    current = await _asset(session, user, payload.current_asset_id)
    wall = [
        await _asset(session, user, asset_id)
        for asset_id in payload.asset_ids
        if asset_id != current.id
    ]

    try:
        record = await sessions.create(session, user.id, current, wall, payload.title)
    except ReferenceTargetGone as exc:
        # 锁内重校验发现素材已被并发删除：与素材不存在同型处理
        raise HTTPException(status.HTTP_404_NOT_FOUND, "素材不存在") from exc
    return await _detail(session, record)


@router.get("")
async def list_sessions(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[SessionOut]:
    records = await sessions.list_for_user(session, user.id, limit)
    return [SessionOut.of(record) for record in records]


@router.get("/{session_id}")
async def get_session(
    session_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> SessionDetailOut:
    return await _detail(session, await _load(session, user, session_id))


@router.patch("/{session_id}")
async def patch_session(
    session_id: uuid.UUID, payload: SessionPatchIn, user: CurrentUser, session: SessionDep
) -> SessionDetailOut:
    record = await _load(session, user, session_id)

    if payload.title is not None:
        record = await sessions.rename(session, record, payload.title)
    if payload.current_asset_id is not None:
        asset = await _asset(session, user, payload.current_asset_id)
        try:
            record = await sessions.switch_current(session, record, asset)
        except ReferenceTargetGone as exc:
            # 引用写入与素材删除竞争，删除先提交：写入被拒绝（PRD §7.1）
            raise HTTPException(status.HTTP_404_NOT_FOUND, "素材不存在") from exc

    return await _detail(session, record)


async def _selection_out(session: AsyncSession, user: User, payload: dict) -> SelectionOut:
    mask = await _asset(session, user, uuid.UUID(payload["mask_asset_id"]))
    return SelectionOut(
        revision=payload["revision"],
        mask=AssetOut.of(mask),
        markers=payload.get("markers") or [],
    )


@router.post("/{session_id}/selection/prepare", status_code=status.HTTP_204_NO_CONTENT)
async def prepare_selection(
    session_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> None:
    record = await _load(session, user, session_id)
    await selections.prepare(session, record)


@router.post("/{session_id}/selection")
async def create_selection(
    session_id: uuid.UUID, payload: SelectIn, user: CurrentUser, session: SessionDep
) -> SelectionOut:
    record = await _load(session, user, session_id)
    try:
        if payload.points:
            stored = await selections.select_points(
                session,
                record,
                payload.revision,
                [(point.x, point.y) for point in payload.points],
                append=payload.append,
            )
        elif payload.strokes:
            stored = await selections.select_strokes(
                session,
                record,
                payload.revision,
                [[(point.x, point.y) for point in stroke] for stroke in payload.strokes],
                payload.radius,
            )
        else:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "请点选或涂抹选区")
    except StaleSelection as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "画布已更新，请重新选择") from exc
    except EmptySelection as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "选区为空") from exc
    return await _selection_out(session, user, stored)


@router.get("/{session_id}/selection")
async def get_selection(
    session_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> SelectionOut | None:
    record = await _load(session, user, session_id)
    stored = await selections.get(record.id, record.revision)
    if stored is None:
        return None
    return await _selection_out(session, user, stored)


@router.delete("/{session_id}/selection", status_code=status.HTTP_204_NO_CONTENT)
async def delete_selection(
    session_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> None:
    record = await _load(session, user, session_id)
    await selections.clear(record.id)


@router.post("/{session_id}/tools", status_code=status.HTTP_202_ACCEPTED)
async def invoke_tool(
    session_id: uuid.UUID, payload: ToolInvokeIn, user: CurrentUser, session: SessionDep
) -> ToolInvokeOut:
    record = await _load(session, user, session_id)
    try:
        run = await tools.submit(session, user.id, payload.tool, payload.params, record.id)
    except UnknownTool as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except InvalidParams as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except SessionNotFound as exc:
        # 登记临界区内会话已被并发删除：任务不入队（PRD §7.1）
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在") from exc
    record = await sessions.get_for_user(session, record.id, user.id)
    return ToolInvokeOut(run=RunOut.of(run), session=await _detail(session, record))


@router.post("/{session_id}/undo")
async def undo_session(
    session_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> SessionDetailOut:
    record = await _load(session, user, session_id)
    try:
        record = await sessions.undo(session, record)
    except CannotUndo as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "没有可撤销的操作") from exc
    return await _detail(session, record)


@router.post("/{session_id}/redo")
async def redo_session(
    session_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> SessionDetailOut:
    record = await _load(session, user, session_id)
    try:
        record = await sessions.redo(session, record)
    except CannotRedo as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "没有可重做的操作") from exc
    return await _detail(session, record)


@router.get("/{session_id}/history")
async def get_history(
    session_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> list[HistoryOut]:
    record = await _load(session, user, session_id)
    entries = await sessions.history_of(session, record)
    return [HistoryOut.of(entry) for entry in entries]


@router.post("/{session_id}/exports")
async def export_session(
    session_id: uuid.UUID, payload: ExportIn, user: CurrentUser, session: SessionDep
) -> Response:
    record = await _load(session, user, session_id)
    try:
        packed = await exports.pack(session, record, payload.asset_ids)
    except UnknownExportAsset as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "素材不在该会话中") from exc
    return Response(
        content=packed.data,
        media_type="application/zip",
        headers={"Content-Disposition": packed.disposition},
    )


@router.get("/{session_id}/messages")
async def list_messages(
    session_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> list[TurnOut]:
    record = await _load(session, user, session_id)
    return [TurnOut.of(turn) for turn in await agent_service.turns_of(session, record)]


@router.post("/{session_id}/messages", status_code=status.HTTP_201_CREATED)
async def send_message(
    session_id: uuid.UUID, payload: MessageIn, user: CurrentUser, session: SessionDep
) -> TurnOut:
    record = await _load(session, user, session_id)
    return TurnOut.of(await agent_service.respond(session, record, payload.text))


@router.post("/{session_id}/messages/{turn_id}/confirm")
async def confirm_plan(
    session_id: uuid.UUID, turn_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> TurnOut:
    record = await _load(session, user, session_id)
    try:
        return TurnOut.of(await agent_service.confirm(session, record, turn_id))
    except TurnNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "对话不存在") from exc
    except CannotConfirm as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "当前没有待确认的步骤") from exc


@router.post("/{session_id}/messages/{turn_id}/cancel")
async def cancel_plan(
    session_id: uuid.UUID, turn_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> TurnOut:
    record = await _load(session, user, session_id)
    try:
        return TurnOut.of(await agent_service.cancel(session, record, turn_id))
    except TurnNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "对话不存在") from exc
    except CannotCancel as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "当前没有可取消的步骤") from exc


@router.post("/{session_id}/messages/{turn_id}/retry")
async def retry_plan(
    session_id: uuid.UUID, turn_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> TurnOut:
    record = await _load(session, user, session_id)
    try:
        return TurnOut.of(await agent_service.retry(session, record, turn_id))
    except TurnNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "对话不存在") from exc
    except CannotRetry as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "没有失败的步骤可重试") from exc


# ---- 创意库 LIB：会话删除预览与删除（PRD §8 / LIB-FR-004、015）----


@router.get("/{session_id}/deletion-preview")
async def preview_session_deletion(
    session_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> SessionDeletionPreview:
    try:
        record = await sessions.get_for_user(session, session_id, user.id)
    except SessionNotFound as exc:
        # 不区分不存在与非本人资源（LIB-FR-011）
        raise _deletion_error(status.HTTP_404_NOT_FOUND, "not_found", "资源不存在") from exc
    try:
        return await sessions.deletion_preview_for_user(session, record)
    except ReferenceStateInvalid as exc:
        raise _deletion_error(
            status.HTTP_409_CONFLICT,
            "reference_state_invalid",
            "引用数据无法安全解析，请先修复数据后再删除",
        ) from exc


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    x_deletion_preview: Annotated[str | None, Header()] = None,
) -> Response:
    # 归属校验在先（404），token 绑定与签名校验在后（LIB-FR-015）
    try:
        await sessions.get_for_user(session, session_id, user.id)
    except SessionNotFound as exc:
        raise _deletion_error(status.HTTP_404_NOT_FOUND, "not_found", "资源不存在") from exc

    if not x_deletion_preview:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "缺少删除确认凭证")
    try:
        expect = read_deletion_token(x_deletion_preview)
    except PreviewTokenError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "删除确认凭证无效") from exc
    except PreviewTokenExpired as exc:
        raise _deletion_error(
            status.HTTP_409_CONFLICT, "delete_preview_expired", "确认凭证已过期，请刷新预览后重试"
        ) from exc
    if expect.get("u") != str(user.id) or expect.get("s") != str(session_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "删除确认凭证与该会话不匹配")

    try:
        keys = await sessions.delete_for_user(session, user.id, session_id, expect=expect)
    except SessionNotFound as exc:
        raise _deletion_error(status.HTTP_404_NOT_FOUND, "not_found", "资源不存在") from exc
    except ActiveTasks as exc:
        raise _deletion_error(
            status.HTTP_409_CONFLICT, "active_tasks", str(exc) or "有任务或计划未结束"
        ) from exc
    except ReferenceStateInvalid as exc:
        raise _deletion_error(
            status.HTTP_409_CONFLICT,
            "reference_state_invalid",
            "引用数据无法安全解析，请先修复数据后再删除",
        ) from exc
    except PreviewScopeChanged as exc:
        raise _deletion_error(
            status.HTTP_409_CONFLICT,
            "delete_preview_changed",
            "删除范围已变化，请刷新预览后重新确认",
        ) from exc

    # 对象清理在 DB 提交之后执行；失败仅告警，不回滚业务删除（LIB-FR-017）
    for asset_id, key in keys:
        await asset_service.cleanup_object(key, asset_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
