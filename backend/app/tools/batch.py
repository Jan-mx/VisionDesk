from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool_run import ToolRun
from app.schemas.batch import BatchIn
from app.services import batch
from app.tools.base import ToolSpec

BATCH_NAME = "batch_process"


async def batch_process_exec(session: AsyncSession, run: ToolRun) -> dict:
    return await batch.execute(session, run)


BATCH_PROCESS = ToolSpec(
    name=BATCH_NAME,
    label="批量处理",
    description=(
        "对多张图执行同一套像素处理：去背景、换背景、调色、超分、扩图、投放尺寸。"
        '对图片墙全部/这些图处理时填 assets="wall"，由服务端解析真实素材；'
        "不要用于单张精修，也不要做营销图、局部编辑或拆层。"
    ),
    params=BatchIn,
    handler=batch_process_exec,
    queued=True,
    session_required=False,
    agent_hidden=("asset_ids",),
    detail=lambda params: (
        f"批量处理 {len(params.get('asset_ids') or [])} 张图"
        if params.get("asset_ids")
        else None
    ),
)
