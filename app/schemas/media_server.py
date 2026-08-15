"""媒体服务器（MediaServerInstance / MediaServerBinding）Pydantic schemas。

docs/design/file-organization.md「API · Media Servers」（R2）：服务器 CRUD
+ test 连通性 + scan 扫描派生 Library；bindings 内嵌在 create/update 中
整体替换。token 不回显（对齐 DownloaderResponse 不 echo password 惯例）。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import ORMModel
from app.utils.download_paths import validate_download_subdir

MEDIA_SERVER_TYPES = ("plex", "emby", "jellyfin")


class MediaServerBindingIn(BaseModel):
    """绑定条目（create/update 内嵌，整体替换语义）。"""

    server_path_prefix: str = Field(min_length=1, max_length=1024)
    volume_id: str
    # 卷内相对路径（"" = 卷根）；校验规则同 Agent.download_subdir。
    subpath: str = ""

    @model_validator(mode="after")
    def _validate(self) -> "MediaServerBindingIn":
        self.server_path_prefix = self.server_path_prefix.strip().rstrip("/")
        if not self.server_path_prefix:
            raise ValueError("server_path_prefix 不能为空")
        self.subpath = validate_download_subdir(self.subpath) or ""
        return self


class MediaServerBindingOut(ORMModel):
    id: str
    server_path_prefix: str
    volume_id: str
    subpath: str


class MediaServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    type: Literal["plex", "emby", "jellyfin"]
    url: str = Field(min_length=1, max_length=2048)
    token: str | None = None
    enabled: bool = True
    bindings: list[MediaServerBindingIn] = []


class MediaServerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    type: Literal["plex", "emby", "jellyfin"] | None = None
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    # None = 保持存储值（表单不回显凭证，对齐 DownloaderUpdate.password）。
    token: str | None = None
    enabled: bool | None = None
    # None = 不动；数组（含空）= 整体替换。
    bindings: list[MediaServerBindingIn] | None = None


class MediaServerOut(BaseModel):
    """详情/列表响应：token 不回显；bindings 内嵌。"""

    id: str
    name: str
    type: str
    url: str
    enabled: bool
    bindings: list[MediaServerBindingOut] = []
    created_at: datetime
    updated_at: datetime


class MediaServerListItem(MediaServerOut):
    """列表项附加派生 Library 计数与待绑定计数。"""

    library_count: int = 0
    unbound_library_count: int = 0


class MediaServerTestResult(BaseModel):
    ok: bool
    server_version: str | None = None
    message: str | None = None


class MediaServerTestRequest(BaseModel):
    """可选覆盖值，供编辑表单按未保存的表单值探测（缺省字段回退已存值）。

    ``token`` 为 None 表示沿用已存凭证（表单不回显 token，留空 = 不修改）。
    """

    type: Literal["plex", "emby", "jellyfin"] | None = None
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    token: str | None = None


class MediaServerTestPayload(BaseModel):
    """无 id 的连通性探测（创建表单用）：直接以给定值构造临时连接目标。"""

    type: Literal["plex", "emby", "jellyfin"]
    url: str = Field(min_length=1, max_length=2048)
    token: str | None = None


class MediaServerScanResult(BaseModel):
    created: int
    updated: int
    unbound: int
