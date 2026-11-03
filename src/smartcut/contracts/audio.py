"""多轨音频契约：资产引用与输出时间线上的音轨。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from smartcut.contracts.ids import SafeId
from smartcut.contracts.time import RationalTime


def _zero() -> RationalTime:
    return RationalTime.from_seconds("0", rate=90_000)


class AudioAssetRef(BaseModel):
    """编译期可解析的音频资产；路径必须位于项目工作区内。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str = ""
    src: str
    duration: RationalTime
    channels: int = Field(default=2, ge=1, le=32)
    sample_rate: int = Field(default=48_000, ge=8_000, le=384_000)
    has_audio: bool = True
    source_hash: str | None = None


class AudioTrackSpec(BaseModel):
    """一条音轨在输出时间线上的放置与音量控制。"""

    model_config = ConfigDict(extra="forbid")

    id: SafeId
    role: Literal[
        "main",
        "voice",
        "music",
        "stem_vocals",
        "stem_instrumental",
        "other",
    ] = "other"
    asset_id: str = "main"
    name: str | None = None
    source_start: RationalTime = Field(default_factory=_zero)
    source_end: RationalTime | None = None
    timeline_start: RationalTime = Field(default_factory=_zero)
    gain_db: float = Field(default=0.0, ge=-60.0, le=12.0, allow_inf_nan=False)
    muted: bool = False
    solo: bool = False
    loop: bool = False
    fade_in: RationalTime = Field(default_factory=_zero)
    fade_out: RationalTime = Field(default_factory=_zero)
    # BGM 自动闪避：以人声/主音轨为 sidechain 压低音乐。
    ducking: bool = False
    ducking_gain_db: float = Field(default=0.0, ge=-24.0, le=12.0, allow_inf_nan=False)
    ducking_ratio: float = Field(default=4.0, ge=1.0, le=20.0, allow_inf_nan=False)
    ducking_threshold: float = Field(default=0.05, gt=0.0, le=1.0, allow_inf_nan=False)
    ducking_attack_ms: int = Field(default=20, ge=1, le=2000)
    ducking_release_ms: int = Field(default=300, ge=10, le=9000)

    @model_validator(mode="after")
    def _ordered(self) -> "AudioTrackSpec":
        if self.source_start.as_fraction() < 0:
            raise ValueError("音轨 source_start 不能为负")
        if (
            self.source_end is not None
            and self.source_end.as_fraction() <= self.source_start.as_fraction()
        ):
            raise ValueError("音轨 source_end 必须大于 source_start")
        if self.timeline_start.as_fraction() < 0:
            raise ValueError("音轨 timeline_start 不能为负")
        if self.fade_in.as_fraction() < 0 or self.fade_out.as_fraction() < 0:
            raise ValueError("音轨淡入/淡出不能为负")
        return self


# 对外语义名称；内部保留 Ref/Spec 以区分资产引用与 IR 音轨。
AudioAsset = AudioAssetRef
AudioTrack = AudioTrackSpec
