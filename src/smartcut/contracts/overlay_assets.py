"""项目级图片/Logo 水印资产。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.ids import SafeId


class OverlayAssetRef(BaseModel):
    """编译期可解析的图片水印资产；路径必须位于项目工作区内。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: SafeId
    name: str = ""
    src: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    source_hash: str | None = None
