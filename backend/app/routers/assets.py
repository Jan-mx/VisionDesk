import uuid
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response

from app.db import SessionDep
from app.deps import CurrentUser
from app.models.asset import AssetKind, AssetSource
from app.schemas.asset import AssetOut, AssetPatchIn, LibraryGroupOut
from app.schemas.deletion import (
    BatchDeleteIn,
    BatchDeleteOut,
    BatchSkippedItem,
    DeletionErrorDetail,
)
from app.services import assets as asset_service
from app.services.assets import ORPHAN_TITLE
from app.services.images import MAX_FILE_BYTES, ImageRejected, probe
from app.services.runs import ActiveTasks
from app.services.sessions import ReferenceStateInvalid

router = APIRouter(prefix="/assets", tags=["assets"])


def _not_found() -> HTTPException:
    """新端点统一 404：不区分不存在与非本人资源（LIB-FR-011）。"""
    detail = DeletionErrorDetail(code="not_found", message="资源不存在")
    return HTTPException(
        status.HTTP_404_NOT_FOUND, detail=detail.model_dump(mode="json", exclude_none=True)
    )


def _conflict(code: str, message: str, references: list | None = None) -> HTTPException:
    detail = DeletionErrorDetail(code=code, message=message, references=references)
    return HTTPException(
        status.HTTP_409_CONFLICT, detail=detail.model_dump(mode="json", exclude_none=True)
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload(
    user: CurrentUser,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> AssetOut:
    data = await file.read()
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "文件超过 20 MB 上限")

    try:
        meta = probe(data)
    except ImageRejected as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    asset = await asset_service.create_from_bytes(
        session, user.id, data, AssetKind.ORIGINAL, AssetSource.UPLOAD, meta
    )
    return AssetOut.of(asset)


@router.get("")
async def list_assets(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AssetOut]:
    records = await asset_service.list_for_user(session, user.id, limit)
    return [AssetOut.of(asset) for asset in records]


@router.get("/library")
async def list_library(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[LibraryGroupOut]:
    groups = await asset_service.library_for_user(session, user.id, limit)
    return [
        LibraryGroupOut(
            session_id=record.id if record else None,
            title=record.title if record else ORPHAN_TITLE,
            updated_at=record.updated_at if record else cover.created_at,
            cover=AssetOut.of(cover),
            assets=[AssetOut.of(asset) for asset in assets],
        )
        for record, cover, assets in groups
    ]


@router.post("/batch-delete")
async def batch_delete_assets(
    payload: BatchDeleteIn, user: CurrentUser, session: SessionDep
) -> BatchDeleteOut:
    """批量删除（LIB-FR-008 / PRD §8.2）：去重保序后请求级校验 + 逐项独立事务。"""
    # 静态路由声明在 /{asset_id} 之前（PRD §8）
    unique = list(dict.fromkeys(payload.asset_ids))
    try:
        deleted, skipped = await asset_service.batch_delete_for_user(session, user.id, unique)
    except asset_service.AssetNotFound as exc:
        raise _not_found() from exc
    except ActiveTasks as exc:
        raise _conflict("active_tasks", str(exc) or "有任务或计划未结束") from exc
    return BatchDeleteOut(
        deleted=deleted,
        skipped=[BatchSkippedItem(**item) for item in skipped],
    )


@router.patch("/{asset_id}")
async def rename_asset(
    asset_id: uuid.UUID, payload: AssetPatchIn, user: CurrentUser, session: SessionDep
) -> AssetOut:
    """素材重命名（LIB-FR-006）：trim 后空白归一 null，超 80 码点 422。"""
    asset = await asset_service.get_for_user(session, user.id, asset_id)
    if asset is None:
        raise _not_found()
    return AssetOut.of(await asset_service.rename(session, asset, payload.title))


@router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset(
    asset_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Response:
    """素材单删（LIB-FR-007）：R(X) 全量引用保护 + 活动任务门禁。"""
    try:
        key = await asset_service.delete_for_user(session, user.id, asset_id)
    except asset_service.AssetNotFound as exc:
        raise _not_found() from exc
    except asset_service.UnsupportedAssetKind as exc:
        raise _conflict(
            "unsupported_asset_kind", "会话内部派生素材，本期不支持删除"
        ) from exc
    except ActiveTasks as exc:
        raise _conflict("active_tasks", str(exc) or "有任务或计划未结束") from exc
    except asset_service.AssetInUse as exc:
        raise _conflict(
            "asset_in_use",
            "素材仍被会话的原图、当前图、图层或编辑历史引用，请先删除对应会话",
            exc.references,
        ) from exc
    except ReferenceStateInvalid as exc:
        raise _conflict(
            "reference_state_invalid", "引用数据无法安全解析，请先修复数据后再删除"
        ) from exc
    # 对象清理在 DB 提交之后执行；失败仅告警（LIB-FR-017）
    await asset_service.cleanup_object(key, asset_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{asset_id}")
async def get_asset(asset_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> AssetOut:
    asset = await asset_service.get_for_user(session, user.id, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "素材不存在")
    return AssetOut.of(asset)
