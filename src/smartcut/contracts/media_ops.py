"""媒体修复/转换契约：诊断、修复请求与自然语言意图草稿。

与 EditIR 解耦：这里只描述“对某个工作区媒体文件做一次修复/转换”，
不包含时间线语义，也不允许模型输出路径、命令或 ffmpeg 参数。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.media import MediaFacts
from smartcut.contracts.transcode import (
    AudioCodec,
    AudioOutput,
    Container,
    TranscodeProfile,
    TranscodeRequest,
    VideoCodec,
)

RepairStrategy = Literal["auto", "remux", "reencode", "audio_sync", "sharpen"]
IssueSeverity = Literal["info", "warning", "error"]
MediaOperation = Literal["diagnose", "repair", "convert", "compress"]


def default_repair_profile() -> TranscodeProfile:
    """修复默认重编参数；实际执行前服务会按源 facts 调整音频/容器。"""
    return TranscodeProfile(
        video_codec="libx264",
        preset="medium",
        crf=20,
        audio_tracks=[AudioOutput(codec="aac", bitrate="160k")],
    )


class MediaIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64)
    severity: IssueSeverity = "warning"
    message: str = Field(min_length=1, max_length=500)
    detail: dict[str, float | int | str | None] = Field(default_factory=dict)


class MediaDiagnosis(BaseModel):
    """一次 probe/缓存质量诊断；不写 IR，也不修改源文件。"""

    model_config = ConfigDict(extra="forbid")

    src: str
    probe_ok: bool
    facts: MediaFacts | None = None
    issues: list[MediaIssue] = Field(default_factory=list)
    recommended: RepairStrategy = "auto"
    can_repair: bool = True
    warnings: list[str] = Field(default_factory=list)


class RepairRequest(BaseModel):
    """一次修复请求；src/dst 必须是工作区相对路径（None=项目主素材）。"""

    model_config = ConfigDict(extra="forbid")

    src: str | None = Field(default=None, description="工作区相对路径；None=项目主素材")
    dst: str | None = Field(default=None, description="工作区相对输出路径")
    strategy: RepairStrategy = "auto"
    profile: TranscodeProfile = Field(default_factory=default_repair_profile)
    audio_offset_seconds: float | None = Field(
        default=None, ge=-60.0, le=60.0
    )
    sharpen_strength: float = Field(default=0.5, ge=0.0, le=1.0)


class MediaGoalDraft(BaseModel):
    """规则或 LLM 的意图草稿；刻意不含 src/dst/路径/命令字段。"""

    model_config = ConfigDict(extra="forbid")

    operation: MediaOperation | None = None
    strategy: RepairStrategy = "auto"
    target_container: Container | None = None
    target_video_codec: VideoCodec | None = None
    target_audio_codec: AudioCodec | None = None
    crf: int | None = Field(default=None, ge=0, le=63)
    max_height: int | None = Field(default=None, ge=2, le=16384)
    audio_offset_seconds: float | None = Field(
        default=None, ge=-60.0, le=60.0
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = Field(default="", max_length=200)


class MediaOperationPlan(BaseModel):
    """服务端补全 facts 后的可执行计划；LLM 不直接产出该对象。"""

    model_config = ConfigDict(extra="forbid")

    operation: MediaOperation
    source: str | None = None
    reason: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    repair: RepairRequest | None = None
    transcode: TranscodeRequest | None = None
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "IssueSeverity",
    "MediaDiagnosis",
    "MediaGoalDraft",
    "MediaIssue",
    "MediaOperation",
    "MediaOperationPlan",
    "RepairRequest",
    "RepairStrategy",
    "default_repair_profile",
]
