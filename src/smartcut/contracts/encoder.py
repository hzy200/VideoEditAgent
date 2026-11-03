"""编码器契约：只允许白名单 H.264 编码器。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EncoderCodec = Literal["libx264", "h264_nvenc", "h264_qsv", "h264_amf"]

ENCODER_CODECS: tuple[str, ...] = (
    "libx264",
    "h264_nvenc",
    "h264_qsv",
    "h264_amf",
)


class EncoderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    codec: EncoderCodec = "libx264"
    preset: str = Field(default="veryfast", min_length=1, max_length=32)
    quality: int = Field(default=18, ge=0, le=51)

    @property
    def hardware(self) -> bool:
        return self.codec != "libx264"


def video_encoder_args(profile: EncoderProfile) -> list[str]:
    """编码器 → ffmpeg 视频参数；纯函数，禁止在这里启动进程。"""
    preset = profile.preset
    quality = str(profile.quality)
    if profile.codec == "libx264":
        return ["-c:v", "libx264", "-preset", preset, "-crf", quality]
    if profile.codec == "h264_nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            preset,
            "-rc",
            "vbr",
            "-cq",
            quality,
            "-b:v",
            "0",
        ]
    if profile.codec == "h264_qsv":
        return [
            "-c:v",
            "h264_qsv",
            "-preset",
            preset,
            "-global_quality",
            quality,
        ]
    if profile.codec == "h264_amf":
        return [
            "-c:v",
            "h264_amf",
            "-quality",
            preset,
            "-rc",
            "cqp",
            "-qp_i",
            quality,
            "-qp_p",
            quality,
            "-qp_b",
            quality,
        ]
    raise ValueError(f"未支持的编码器: {profile.codec}")
