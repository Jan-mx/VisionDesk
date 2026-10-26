"""管理端用户维护契约（WI-20261025-02 实现要求 4/7/8，PRD §8）。

AdminUserOut 不暴露 password_hash/token_version；PATCH schema extra=forbid 使
role/token_version/last_login_at 等注入一律 422（ADM-AC-020）。
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.account_deletion_operation import AccountDeletionStatus
from app.models.asset import AssetKind, AssetSource
from app.models.tool_run import RunStatus
from app.models.user import UserRole, UserStatus
from app.schemas.asset import TITLE_MAX_CODEPOINTS
from app.schemas.auth import Credentials, PasswordStr, UsernameStr, normalize_username
from app.schemas.deletion import DeletionBlocker, ReferenceOut
from app.services.paging import PageParams


class AdminUserOut(BaseModel):
    """管理端用户视图：含统计聚合（列表与详情一致，WI 实现要求 7/8）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    role: UserRole
    status: UserStatus
    created_at: datetime
    last_login_at: datetime | None
    asset_count: int
    asset_record_bytes: int
    session_count: int
    tool_run_count: int
    agent_run_count: int


class AdminUserCreate(Credentials):
    """创建普通账号：与注册同一套凭据校验与唯一性（WI 实现要求 4）。"""


class AdminUserPatch(BaseModel):
    """改名与启停：只允许 username / status(active|disabled) 二选一或并列。

    extra="forbid"：role、token_version、last_login_at、deleting 等字段一律 422，
    普通账号不能由此获得管理员身份、状态机不能被绕过（PRD §8.1）。
    """

    model_config = ConfigDict(extra="forbid")

    username: UsernameStr | None = None
    status: Literal["active", "disabled"] | None = None

    @field_validator("username")
    @classmethod
    def _normalize(cls, value: str | None) -> str | None:
        return None if value is None else normalize_username(value)

    @model_validator(mode="after")
    def _requires_a_field(self) -> "AdminUserPatch":
        if self.username is None and self.status is None:
            raise ValueError("至少提供 username 或 status 之一")
        return self


class ResetPasswordInput(BaseModel):
    """重置密码专用输入：与注册同一套密码长度约束（WI 实现要求 4）。"""

    model_config = ConfigDict(extra="forbid")

    password: PasswordStr


class UserListParams(PageParams):
    """GET /admin/users 查询参数。

    sort 白名单 created_at/username/status（Literal）；时间筛选必须带时区
    （AwareDatetime，无时区 422），区间反向 422（WI 实现要求 3/4）。
    """

    sort: Literal["created_at", "username", "status"] = "created_at"
    q: Annotated[str | None, Field(max_length=64)] = None
    status: UserStatus | None = None
    created_from: AwareDatetime | None = None
    created_to: AwareDatetime | None = None

    @model_validator(mode="after")
    def _time_range_order(self) -> "UserListParams":
        if self.created_from and self.created_to and self.created_from > self.created_to:
            raise ValueError("created_from 不得晚于 created_to")
        return self


# ---- 账号删除（WI-20261025-03 实现要求 3/7，PRD §8.3）----


class DeletionCounts(BaseModel):
    """预览计数：全量口径，不受列表页 limit、可见 kind 或隐藏状态影响。"""

    sessions: int
    history: int
    tool_runs: int
    agent_runs: int
    assets: int
    asset_record_bytes: int
    objects: int
    orphan_objects: int


class AccountDeletionPreview(BaseModel):
    """删除预览（ADM-FR-011）：blocked 时 preview_token 为空、blockers 可读。"""

    user_id: uuid.UUID
    username: str
    counts: DeletionCounts
    blocked: bool
    blockers: list[DeletionBlocker]
    preview_token: str | None
    expires_at: datetime | None


class DeletionOperationOut(BaseModel):
    """清理操作视图：不含对象 key 清单（WI 实现要求 7）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    target_user_id: uuid.UUID
    status: AccountDeletionStatus
    phase: str
    progress_done: int
    progress_total: int
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    failure_code: str | None
    failure_detail: str | None


class DeletionConfirmInput(BaseModel):
    """确认删除请求体：管理员须输入目标当前用户名（PRD §6.3.4）。"""

    model_config = ConfigDict(extra="forbid")

    confirm_username: str


# ---- 管理端素材（WI-20261025-04，PRD §6.5/§8.1~8.3）----


class AdminAssetPatchIn(BaseModel):
    """管理端素材重命名（WI 决策 3）：沿用 title 规则——trim、空白归一 null、
    ≤80 Unicode 码点（超长 422 不截断）；title 必填（缺失 422）、非字符串 422。

    注意：先 trim 再校验长度（PRD §6.5 原文「trim、空值归一 null」）——
    用户端 AssetPatchIn 只做空白归一不 trim，管理端不照抄该实现。
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(max_length=TITLE_MAX_CODEPOINTS)

    @field_validator("title", mode="before")
    @classmethod
    def _trim_blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class AdminAssetOut(BaseModel):
    """管理端素材视图（PRD §8.3）：安全元信息（不含 storage_key）+ 归属 +
    引用计数摘要 + 能力与稳定原因码；preview_url 为短期签名 URL（不入审计——
    summary 白名单无此键）。列表与详情共用本模型保证逐字段一致（ADM-AC-036）。"""

    id: uuid.UUID
    kind: AssetKind
    source: AssetSource
    title: str | None
    image_format: str
    width: int
    height: int
    size_bytes: int
    has_alpha: bool
    created_at: datetime
    user_id: uuid.UUID
    username: str
    library_hidden: bool
    references_count: dict[str, int]
    can_rename: bool
    rename_blocker: str | None
    can_delete: bool
    delete_blocker: str | None
    preview_url: str


class AdminAssetDetailOut(AdminAssetOut):
    """素材详情：另返回完整 references（session_id/title/types 引用行）。"""

    references: list[ReferenceOut]


class AdminAssetDeleteOut(BaseModel):
    """单删结果两态（WI 决策 4）：deleted=物理删除；hidden=仅历史引用，
    转隐藏式删除（行与对象保留供撤销/重做）。"""

    id: uuid.UUID
    status: Literal["deleted", "hidden"]


class AdminAssetListParams(PageParams):
    """GET /admin/assets 查询参数（PRD §8.2）：跨用户筛选 + 白名单排序
    （created_at/size_bytes/title）+ 带时区时间区间（下含上不含、反向 422）。"""

    sort: Literal["created_at", "size_bytes", "title"] = "created_at"
    user_id: uuid.UUID | None = None
    q: Annotated[str | None, Field(max_length=100)] = None
    kind: AssetKind | None = None
    source: AssetSource | None = None
    library_hidden: bool | None = None
    created_from: AwareDatetime | None = None
    created_to: AwareDatetime | None = None

    @model_validator(mode="after")
    def _time_range_order(self) -> "AdminAssetListParams":
        if self.created_from and self.created_to and self.created_from > self.created_to:
            raise ValueError("created_from 不得晚于 created_to")
        return self


# ---- 任务与操作查询（WI-20261025-05，PRD §6.5/§8.1~8.3；ADM-FR-021/022/024）----


class AdminRunStepOut(BaseModel):
    """plan 步骤摘要（WI 决策 3）：每步仅 tool 名与 status，params 不透传。"""

    tool: str
    status: str


class AdminToolRunOut(BaseModel):
    """管理端工具任务视图（WI 决策 2 白名单）：params/result 原始 JSON 不透传
    （可能含提示词）；时间字段如实（可空）；duration_ms 仅当起止均存在才计算、
    否则 null 不编造。"""

    id: uuid.UUID
    user_id: uuid.UUID
    username: str
    session_id: uuid.UUID | None
    tool: str
    status: RunStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None
    duration_ms: int | None


class AdminToolRunDetailOut(AdminToolRunOut):
    """工具任务详情（WI 决策 2/5）：另含计划步骤（工具任务无则空）、产物列表
    （存在 → {asset_id, kind, url 短期签名}；素材行已删 → {asset_id,
    missing: true}）与脱敏 error 摘要；error 仅详情返回（列表白名单不含）。"""

    steps: list[AdminRunStepOut] = []
    artifacts: list[dict[str, Any]] = []
    error: str | None = None


class AdminAgentRunOut(BaseModel):
    """管理端 Agent 轮次视图（WI 决策 3 白名单）：goal/reply/原始 JSON/供应商
    返回一律不透传；时间只有 created_at——不编造开始/结束/耗时；turn 为该会话
    (created_at, id) 次序的 1 基轮次序号（如实推算，模型无持久化轮次列）。"""

    id: uuid.UUID
    user_id: uuid.UUID
    username: str
    session_id: uuid.UUID
    status: RunStatus
    created_at: datetime
    turn: int
    steps: list[AdminRunStepOut] = []
    artifacts: list[dict[str, Any]] = []


class AdminAgentRunDetailOut(AdminAgentRunOut):
    """轮次详情：另含脱敏 error 摘要（WI 决策 5；列表白名单不含 error）。"""

    error: str | None = None


class AdminAuditLogOut(BaseModel):
    """操作记录视图（WI 决策 4）：actor_username 由 join 取得、actor 已删为
    null；summary 为白名单脱敏摘要，原样返回。"""

    id: uuid.UUID
    actor_id: uuid.UUID
    actor_username: str | None = None
    target_type: str
    target_id: uuid.UUID
    action: str
    result: str
    request_id: str | None
    summary: dict[str, Any] | None
    operation_id: uuid.UUID | None
    created_at: datetime


class AdminAuditLogDetailOut(AdminAuditLogOut):
    """操作记录详情（WI 决策 4）：operation_id 非空时嵌套清理操作视图
    （读操作详情不受清理开关限制；不含 scope_digest 原始对象清单）。"""

    operation: DeletionOperationOut | None = None


class AdminToolRunListParams(PageParams):
    """GET /admin/tool-runs 查询参数（PRD §8.2）：user_id/status/tool/时间区间
    筛选 + 排序白名单 created_at/status/tool；时间须带时区、反向区间 422。"""

    sort: Literal["created_at", "status", "tool"] = "created_at"
    user_id: uuid.UUID | None = None
    status: RunStatus | None = None
    tool: Annotated[str | None, Field(max_length=48)] = None
    q: Annotated[str | None, Field(max_length=100)] = None
    created_from: AwareDatetime | None = None
    created_to: AwareDatetime | None = None

    @model_validator(mode="after")
    def _time_range_order(self) -> "AdminToolRunListParams":
        if self.created_from and self.created_to and self.created_from > self.created_to:
            raise ValueError("created_from 不得晚于 created_to")
        return self


class AdminAgentRunListParams(PageParams):
    """GET /admin/agent-runs 查询参数（PRD §8.2）：user_id/status/session_id/
    时间区间筛选 + 排序白名单 created_at/status（无 tool——类型由资源决定）。"""

    sort: Literal["created_at", "status"] = "created_at"
    user_id: uuid.UUID | None = None
    status: RunStatus | None = None
    session_id: uuid.UUID | None = None
    created_from: AwareDatetime | None = None
    created_to: AwareDatetime | None = None

    @model_validator(mode="after")
    def _time_range_order(self) -> "AdminAgentRunListParams":
        if self.created_from and self.created_to and self.created_from > self.created_to:
            raise ValueError("created_from 不得晚于 created_to")
        return self


class AdminAuditLogListParams(PageParams):
    """GET /admin/audit-logs 查询参数（PRD §8.2）：actor_id/target_id/action/
    result/时间区间筛选 + 排序白名单 created_at/action/result；无筛选默认
    不截断（total 为全表真实总数）。"""

    sort: Literal["created_at", "action", "result"] = "created_at"
    actor_id: uuid.UUID | None = None
    target_id: uuid.UUID | None = None
    action: Annotated[str | None, Field(max_length=32)] = None
    result: Annotated[str | None, Field(max_length=16)] = None
    created_from: AwareDatetime | None = None
    created_to: AwareDatetime | None = None

    @model_validator(mode="after")
    def _time_range_order(self) -> "AdminAuditLogListParams":
        if self.created_from and self.created_to and self.created_from > self.created_to:
            raise ValueError("created_from 不得晚于 created_to")
        return self
