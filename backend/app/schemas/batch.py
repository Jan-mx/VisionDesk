import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.asset import AssetOut
from app.schemas.run import RunOut

BATCH_TOOLS = (
    "remove_background",
    "replace_background",
    "adjust_image",
    "upscale_image",
    "expand_canvas",
    "prepare_delivery_sizes",
)

MAX_BATCH_ASSETS = 20
ExportFormat = Literal["png", "jpg"]


class BatchOpIn(BaseModel):
    tool: Literal[
        "remove_background",
        "replace_background",
        "adjust_image",
        "upscale_image",
        "expand_canvas",
        "prepare_delivery_sizes",
    ]
    params: dict = Field(default_factory=dict)


class BatchIn(BaseModel):
    """asset_ids 与 assets 二选一：界面直发传 id 列表；聊天规划用 assets="wall" 由服务端解析。"""

    asset_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_BATCH_ASSETS)
    assets: Literal["wall"] | None = None
    operations: list[BatchOpIn] = Field(min_length=1, max_length=len(BATCH_TOOLS))
    formats: list[ExportFormat] = Field(default_factory=lambda: ["png"], min_length=1, max_length=2)

    @model_validator(mode="after")
    def _need_targets(self) -> "BatchIn":
        if not self.asset_ids and not self.assets:
            raise ValueError("需要 asset_ids 或 assets=\"wall\" 之一")
        return self


class BatchItemOut(BaseModel):
    source: AssetOut
    status: str
    error: str | None = None
    outputs: list[AssetOut] = []


class BatchOut(BaseModel):
    run: RunOut
    items: list[BatchItemOut] = []
    created_at: datetime
