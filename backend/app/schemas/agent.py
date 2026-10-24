import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from app.models import AgentRun
from app.models.tool_run import RunStatus
from app.tools import label_of

MAX_MESSAGE = 1000


class ChatContextIn(BaseModel):
    """客户端只提交选择意图；事实（归属/可见性/图层存在）由服务端重建校验。

    selected_asset_ids 放宽到 100：超过批量上限的集合也要进入服务端，
    由解析层给出可读的「缩小范围」提示，而不是 422 一刀切。
    """

    selected_layer_id: str | None = Field(default=None, max_length=100)
    selected_asset_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class MessageIn(BaseModel):
    text: Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE)]
    context: ChatContextIn | None = None

    @field_validator("text")
    @classmethod
    def _require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("指令不能为空")
        return value


class PlanStepOut(BaseModel):
    id: str
    tool: str
    label: str
    depends_on: list[str] = []
    run_id: uuid.UUID | None = None
    status: str = "pending"

    @classmethod
    def of(cls, step: dict) -> "PlanStepOut":
        run_id = step.get("run_id")
        return cls(
            id=step.get("id") or step["tool"],
            tool=step["tool"],
            label=label_of(step["tool"], step.get("params")),
            depends_on=list(step.get("depends_on") or []),
            run_id=uuid.UUID(run_id) if run_id else None,
            status=step.get("status") or ("succeeded" if run_id else "pending"),
        )


class TurnOut(BaseModel):
    id: uuid.UUID
    revision: int
    goal: str
    reply: str
    status: RunStatus
    error: str | None
    created_at: datetime
    steps: list[PlanStepOut] = []

    @classmethod
    def of(cls, turn: AgentRun) -> "TurnOut":
        return cls(
            id=turn.id,
            revision=turn.revision,
            goal=turn.goal,
            reply=turn.reply,
            status=turn.status,
            error=turn.error,
            created_at=turn.created_at,
            steps=[PlanStepOut.of(step) for step in turn.plan],
        )
