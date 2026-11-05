"""时间轴算子：转场与水印叠加。

转场改变输出时长（相邻片段重叠）；水印不改变时长，但需要额外滤镜。
这些都不属于 ``EditIR.effects``。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from smartcut.contracts.ids import SafeId
from smartcut.contracts.time import RationalTime

_RATE = 90_000
_TRANSITIONS = (
    "fade",
    "fadeblack",
    "fadewhite",
    "fadegrays",
    "dissolve",
    "wipeleft",
    "wiperight",
    "wipetl",
    "wipetr",
    "wipebl",
    "wipebr",
    "slideleft",
    "slideright",
    "slideup",
    "slidedown",
    "smoothleft",
    "smoothright",
    "smoothup",
    "smoothdown",
    "circleopen",
    "circleclose",
    "circlecrop",
    "rectcrop",
    "radial",
    "pixelize",
    "hlslice",
    "hrslice",
    "vuslice",
    "vdslice",
    "hblur",
    "squeezeh",
    "squeezev",
    "zoomin",
    "vertopen",
    "vertclose",
    "horzopen",
    "horzclose",
)
TRANSITION_AUDIO_CURVES = (
    "tri",
    "qsin",
    "esin",
    "hsin",
    "log",
    "ipar",
    "qua",
    "cub",
    "squ",
    "par",
    "exp",
    "nofade",
)


def zero_time() -> RationalTime:
    return RationalTime(value=0, rate=_RATE)


class TransitionSpec(BaseModel):
    """在 ``after`` 片段与下一个片段之间插入的重叠转场。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    after: SafeId
    type: Literal[
        "fade",
        "fadeblack",
        "fadewhite",
        "fadegrays",
        "dissolve",
        "wipeleft",
        "wiperight",
        "wipetl",
        "wipetr",
        "wipebl",
        "wipebr",
        "slideleft",
        "slideright",
        "slideup",
        "slidedown",
        "smoothleft",
        "smoothright",
        "smoothup",
        "smoothdown",
        "circleopen",
        "circleclose",
        "circlecrop",
        "rectcrop",
        "radial",
        "pixelize",
        "hlslice",
        "hrslice",
        "vuslice",
        "vdslice",
        "hblur",
        "squeezeh",
        "squeezev",
        "zoomin",
        "vertopen",
        "vertclose",
        "horzopen",
        "horzclose",
    ] = "fade"
    duration: RationalTime
    audio_curve: Literal[
        "tri",
        "qsin",
        "esin",
        "hsin",
        "log",
        "ipar",
        "qua",
        "cub",
        "squ",
        "par",
        "exp",
        "nofade",
    ] = "tri"

    @model_validator(mode="after")
    def _positive(self) -> "TransitionSpec":
        if self.duration.as_fraction() <= 0:
            raise ValueError("转场时长必须大于 0")
        return self


class OverlaySpec(BaseModel):
    """文字或图片水印；叠在成片画面上，不改变时长。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["text", "image"] = "text"
    text: str | None = Field(default=None, max_length=80)
    # 图片水印引用项目 overlay 资产；IR 中不允许出现文件路径。
    asset_id: SafeId | None = None
    # 图片水印宽度占输出画面宽度的比例。
    scale: float = Field(default=0.2, ge=0.02, le=1.0)
    position: Literal[
        "top_left",
        "top_right",
        "bottom_left",
        "bottom_right",
        "center",
    ] = "bottom_right"
    font_size: int = Field(default=28, ge=8, le=120)
    color: str = Field(
        default="#FFFFFF",
        pattern=r"^#[0-9A-Fa-f]{6}$",
    )
    opacity: float = Field(default=0.7, ge=0.0, le=1.0)
    start: RationalTime = Field(default_factory=zero_time)
    end: RationalTime | None = None

    @model_validator(mode="after")
    def _window(self) -> "OverlaySpec":
        if self.start.as_fraction() < 0:
            raise ValueError("水印起点不能为负")
        if self.end is not None and self.end <= self.start:
            raise ValueError("水印结束时间必须大于开始时间")
        if self.kind == "text":
            if not (self.text or "").strip():
                raise ValueError("文字水印必须提供 text")
            if self.asset_id is not None:
                raise ValueError("文字水印不得携带 asset_id")
        else:
            if self.asset_id is None:
                raise ValueError("图片水印必须提供 asset_id")
            if self.text:
                raise ValueError("图片水印不得携带 text")
        return self


class WatermarkRemovalSpec(BaseModel):
    """对源画面矩形区域做 delogo 去除；坐标基于输出画面像素。

    这是 GUI 手动标注能力：模型不得输出像素坐标。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: SafeId
    # ffmpeg delogo 要求区域不接触画面边界。
    x: int = Field(ge=1)
    y: int = Field(ge=1)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    start: RationalTime = Field(default_factory=zero_time)
    end: RationalTime | None = None

    @model_validator(mode="after")
    def _window(self) -> "WatermarkRemovalSpec":
        if self.start.as_fraction() < 0:
            raise ValueError("去水印起点不能为负")
        if self.end is not None and self.end <= self.start:
            raise ValueError("去水印结束时间必须大于开始时间")
        return self


class PipClip(BaseModel):
    """画中画/叠加视频轨：在 timeline_start 起叠加到主轨。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: SafeId
    src: str = Field(min_length=1)
    source_start: RationalTime
    source_end: RationalTime
    timeline_start: RationalTime
    duration: RationalTime
    x: float = Field(default=0.68, ge=0.0, le=1.0)
    y: float = Field(default=0.05, ge=0.0, le=1.0)
    width: float = Field(default=0.28, gt=0.0, le=1.0)
    height: float = Field(default=0.28, gt=0.0, le=1.0)
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    z: int = Field(default=0, ge=-16, le=16)

    @model_validator(mode="after")
    def _window(self) -> "PipClip":
        if self.source_end <= self.source_start:
            raise ValueError("PipClip.source_end 必须大于 source_start")
        if self.timeline_start.as_fraction() < 0:
            raise ValueError("PipClip.timeline_start 不能为负")
        if self.duration.as_fraction() <= 0:
            raise ValueError("PipClip.duration 必须大于 0")
        if self.x + self.width > 1.0 + 1e-6 or self.y + self.height > 1.0 + 1e-6:
            raise ValueError("PipClip 位置/尺寸超出画布")
        return self


TRANSITION_TYPES = _TRANSITIONS
__all__ = [
    "OverlaySpec",
    "PipClip",
    "TRANSITION_AUDIO_CURVES",
    "TRANSITION_TYPES",
    "TransitionSpec",
    "WatermarkRemovalSpec",
    "zero_time",
]
