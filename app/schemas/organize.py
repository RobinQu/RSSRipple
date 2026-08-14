"""Pydantic schemas for the built-in file organization subsystem (organize).

Libraries / OrganizeRules / OrganizePlans / audit entries — see
docs/design/file-organization.md「API」. Pydantic-level validation (Literal
enums) surfaces as 422 VALIDATION_ERROR via the global handler; semantic
validation (DSL, template) happens in the route layer.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import ORMModel

LIBRARY_KINDS = ("tv", "movie", "mixed")
PLAN_STATUSES = ("pending", "running", "done", "failed", "cancelled")


# ---------------------------------------------------------------- Libraries


class LibraryUpdate(BaseModel):
    """Library 收敛为扫描派生（R2）：仅可局部更新。

    ``subtitle_lang_map`` 为 Library 级字幕映射覆盖；``volume_id`` /
    ``root_subpath`` 用于待绑定行就地修复（补绑定）。其余字段由扫描派生，
    提交即 422（extra="forbid"）。
    """

    model_config = ConfigDict(extra="forbid")

    subtitle_lang_map: dict[str, str] | None = None
    volume_id: str | None = None
    root_subpath: str | None = None


class LibraryOut(BaseModel):
    """库详情/列表响应：root_path 为派生展示字段（volume.mount_path +
    root_subpath 解析结果），未绑定卷为 None；bound 为绑定状态。"""

    id: str
    name: str
    kind: str
    media_server_id: str | None
    media_server_name: str | None = None
    section_key: str | None
    server_path: str | None
    volume_id: str | None
    volume_name: str | None = None
    root_subpath: str | None
    root_path: str | None
    bound: bool
    subtitle_lang_map: dict[str, str] | None
    created_at: datetime
    updated_at: datetime


class LibraryListItem(LibraryOut):
    """List item carries the library's pending plan count (small, unpaginated)."""

    pending_plan_count: int = 0


# ---------------------------------------------------------------- Rules


class OrganizeRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    priority: int = 100
    enabled: bool = True
    # BoolCondition DSL 根节点 JSON；null = 匹配全部。语义校验（结构/空
    # value）在路由层走 validate_filter_config。
    filter: dict[str, Any] | None = None
    library_id: str
    path_template: str = Field(min_length=1, max_length=1024)
    # R3 起支持 hardlink/copy（保种语义：源文件保留，执行后不删任务）；
    # 其他值 schema 层即 422。
    file_op: Literal["move", "hardlink", "copy"] = "move"
    auto_execute: bool = False


class OrganizeRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    priority: int | None = None
    enabled: bool | None = None
    filter: dict[str, Any] | None = None
    library_id: str | None = None
    path_template: str | None = Field(default=None, min_length=1, max_length=1024)
    file_op: Literal["move", "hardlink", "copy"] | None = None
    auto_execute: bool | None = None


class OrganizeRuleOut(ORMModel):
    id: str
    name: str
    priority: int
    enabled: bool
    filter: dict[str, Any] | None
    library_id: str
    path_template: str
    file_op: str
    auto_execute: bool
    created_at: datetime
    updated_at: datetime


class OrganizeRuleDraft(BaseModel):
    """Rule draft for the preview endpoint (not persisted)."""

    name: str = "preview"
    priority: int = 0
    enabled: bool = True
    filter: dict[str, Any] | None = None
    library_id: str
    path_template: str = Field(min_length=1, max_length=1024)
    file_op: Literal["move", "hardlink", "copy"] = "move"


# ---------------------------------------------------------------- Preview


class OrganizePreviewRequest(BaseModel):
    """dry-run 预览：notification_id 或 resource_id 二选一，可附规则草稿。"""

    notification_id: str | None = None
    resource_id: str | None = None
    rule: OrganizeRuleDraft | None = None
    category: str | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "OrganizePreviewRequest":
        if self.notification_id is None and self.resource_id is None:
            raise ValueError("notification_id 与 resource_id 至少提供一个")
        if self.notification_id is not None and self.resource_id is not None:
            raise ValueError("notification_id 与 resource_id 只能提供一个")
        return self


class OrganizePreviewOp(BaseModel):
    op_type: str  # move | keep
    src: str
    dst: str | None
    size: int
    reason: str = ""


class OrganizePreviewResponse(BaseModel):
    matched_rule: dict[str, str | None] | None = None  # {id?, name}
    library: dict[str, str] | None = None  # {id, name}
    category: str | None = None
    needs_category: bool = False
    uncategorized: bool = False
    ops: list[OrganizePreviewOp] = []


# ---------------------------------------------------------------- Plans


class OrganizePlanOpOut(ORMModel):
    id: str
    seq: int
    op_type: str  # move | keep | movedir
    src: str
    dst: str | None
    size: int
    status: str
    error_message: str | None


class OrganizePlanListItem(BaseModel):
    """List row: no payload, carries display joins and an ops summary."""

    id: str
    notification_id: str
    rule_id: str | None
    rule_name: str | None = None
    library_id: str | None
    library_name: str | None = None
    category: str | None
    status: str
    error_message: str | None
    executed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    ops_summary: dict[str, int] = {}
    # pending 派生原因（R2）：library 未定 / 模板 {category} 未定 →
    # "unclassified"；目标库未绑定卷 → "unbound"；非 pending 或可执行 → None。
    pending_reason: Literal["unclassified", "unbound"] | None = None


class OrganizeAuditOut(ORMModel):
    id: str
    plan_id: str
    action: str
    detail: dict[str, Any] | None
    created_at: datetime


class OrganizePlanDetail(OrganizePlanListItem):
    payload: dict[str, Any]
    ops: list[OrganizePlanOpOut] = []
    audit_entries: list[OrganizeAuditOut] = []


class OrganizeExecuteBatchRequest(BaseModel):
    plan_ids: list[str] = Field(min_length=1)


class OrganizeClassifyRequest(BaseModel):
    library_id: str
    category: str | None = None
