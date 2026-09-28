import copy

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ToolRun
from app.providers import get_background_removal_provider
from app.providers.background import MattingCheckpoint
from app.services import runs

_CHECKPOINTS = "_background_jobs"


async def remove(
    session: AsyncSession,
    run: ToolRun,
    image: bytes,
    *,
    key: str = "default",
) -> bytes:
    """Run cloud matting while persisting enough state to resume after a worker restart."""
    jobs = (run.result or {}).get(_CHECKPOINTS) or {}
    checkpoint = MattingCheckpoint.from_dict(jobs.get(key))

    async def save(value: MattingCheckpoint) -> None:
        result = copy.deepcopy(run.result or {})
        stored = dict(result.get(_CHECKPOINTS) or {})
        stored[key] = value.as_dict()
        result[_CHECKPOINTS] = stored
        await runs.report(session, run, max(run.progress, 50), "云端任务已提交", result)

    async def progress(value: int, stage: str) -> None:
        await runs.report(session, run, value, stage, run.result or {})

    return await get_background_removal_provider().remove(
        image,
        checkpoint=checkpoint,
        on_checkpoint=save,
        on_progress=progress,
    )
