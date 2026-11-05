"""独立转码/封装契约。

转码不属于 EditIR：它面向“把工作区内的任意媒体按输出 profile 重新封装/编码”，
不参与时间线语义，也不允许模型输出 ffmpeg 参数。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from smartcut.contracts.time import fps_from_string

Container = Literal["mp4", "mov", "mkv", "mp3", "m4a", "wav", "flac"]
MediaType = Literal["video", "audio"]
VideoCodec = Literal["copy", "libx264", "libx265", "libsvtav1", "prores_ks"]
AudioCodec = Literal[
    "copy",
    "aac",
    "ac3",
    "libopus",
    "libmp3lame",
    "flac",
    "pcm_s16le",
    "pcm_s24le",
]
SubtitleCodec = Literal["copy", "mov_text", "srt", "ass", "webvtt"]
ScaleMode = Literal["fit", "stretch", "crop"]
FpsMode = Literal["cfr", "vfr", "passthrough"]

AUDIO_CONTAINERS = frozenset({"mp3", "m4a", "wav", "flac"})
DEFAULT_AUDIO_CODEC: dict[str, AudioCodec] = {
    "mp4": "aac",
    "mov": "aac",
    "mkv": "aac",
    "m4a": "aac",
    "mp3": "libmp3lame",
    "wav": "pcm_s16le",
    "flac": "flac",
}

_CONTAINER_VIDEO: dict[str, set[str]] = {
    "mp4": {"copy", "libx264", "libx265", "libsvtav1"},
    "mov": {"copy", "libx264", "libx265", "prores_ks"},
    "mkv": {"copy", "libx264", "libx265", "libsvtav1", "prores_ks"},
}
_CONTAINER_AUDIO: dict[str, set[str]] = {
    "mp4": {"copy", "aac", "ac3", "libopus"},
    "mov": {"copy", "aac", "ac3", "pcm_s16le", "pcm_s24le"},
    "mkv": {"copy", "aac", "ac3", "libopus", "flac", "pcm_s16le", "pcm_s24le"},
    "m4a": {"copy", "aac", "ac3", "libopus"},
    "mp3": {"copy", "libmp3lame"},
    "wav": {"copy", "pcm_s16le", "pcm_s24le"},
    "flac": {"copy", "flac"},
}
_CONTAINER_SUBTITLE: dict[str, set[str]] = {
    "mp4": {"copy", "mov_text"},
    "mov": {"copy", "mov_text"},
    "mkv": {"copy", "mov_text", "srt", "ass", "webvtt"},
    "m4a": set(),
    "mp3": set(),
    "wav": set(),
    "flac": set(),
}
_RATE = re.compile(r"^\d+(?:\.\d+)?[kKmM]?$")


class AudioOutput(BaseModel):
    """一条输出音轨；src=None 表示主输入的音频流。"""

    model_config = ConfigDict(extra="forbid")

    src: str | None = Field(default=None, description="工作区相对路径；None=主输入")
    stream_index: int = Field(default=0, ge=0, le=127)
    codec: AudioCodec = "copy"
    bitrate: str | None = Field(default=None, pattern=r"^\d+(?:\.\d+)?[kKmM]$")
    channels: int | None = Field(default=None, ge=1, le=32)
    language: str | None = Field(default=None, max_length=16)
    title: str | None = Field(default=None, max_length=200)
    gain_db: float = Field(default=0.0, ge=-60.0, le=12.0, allow_inf_nan=False)


class SubtitleOutput(BaseModel):
    """一条输出字幕轨；src=None 表示主输入的内嵌字幕流。"""

    model_config = ConfigDict(extra="forbid")

    src: str | None = Field(default=None, description="工作区相对 SRT/ASS/VTT")
    stream_index: int = Field(default=0, ge=0, le=127)
    codec: SubtitleCodec = "mov_text"
    language: str | None = Field(default=None, max_length=16)
    title: str | None = Field(default=None, max_length=200)


class MetadataOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=500)
    artist: str | None = Field(default=None, max_length=500)
    album: str | None = Field(default=None, max_length=500)
    comment: str | None = Field(default=None, max_length=2000)
    language: str | None = Field(default=None, max_length=16)
    extra: dict[str, str] = Field(default_factory=dict)

    @field_validator("extra")
    @classmethod
    def _safe_keys(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not key.strip() or "=" in key or "\x00" in key or "\x00" in item:
                raise ValueError(f"非法元数据键/值: {key!r}")
        return value


class TranscodeProfile(BaseModel):
    """输出 profile：只描述解码后的目标格式，不包含任何时间线语义。"""

    model_config = ConfigDict(extra="forbid")

    media_type: MediaType = "video"
    container: Container = "mp4"
    video_codec: VideoCodec = "libx264"
    preset: str = Field(default="medium", min_length=1, max_length=32)
    crf: int | None = Field(default=20, ge=0, le=63)
    video_bitrate: str | None = Field(default=None, pattern=r"^\d+(?:\.\d+)?[kKmM]$")
    max_bitrate: str | None = Field(default=None, pattern=r"^\d+(?:\.\d+)?[kKmM]$")
    width: int | None = Field(default=None, ge=2, le=16384)
    height: int | None = Field(default=None, ge=2, le=16384)
    scale_mode: ScaleMode = "fit"
    fps: str | None = None
    fps_mode: FpsMode = "cfr"
    pix_fmt: str | None = Field(default=None, max_length=32)
    keep_10bit: bool = False
    hdr_tonemap: bool = False
    autorotate: bool = True
    faststart: bool = True
    loudness_target: float | None = Field(default=None, ge=-70.0, le=0.0)
    loudnorm_two_pass: bool = True
    true_peak: float = Field(default=-1.5, ge=-9.0, le=0.0)
    lra: float = Field(default=11.0, ge=1.0, le=50.0)
    audio_tracks: list[AudioOutput] = Field(default_factory=list)
    subtitle_tracks: list[SubtitleOutput] = Field(default_factory=list)
    metadata: MetadataOptions = Field(default_factory=MetadataOptions)

    @field_validator("fps")
    @classmethod
    def _valid_fps(cls, value: str | None) -> str | None:
        if value is None:
            return None
        fps_from_string(value)
        return value

    @field_validator("crf")
    @classmethod
    def _crf_for_h264(cls, value: int | None, info) -> int | None:
        if value is not None and info.data.get("video_codec") == "libx264" and value > 51:
            raise ValueError("libx264 的 crf 必须在 0..51")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "TranscodeProfile":
        container = self.container
        if self.media_type == "audio":
            if container not in AUDIO_CONTAINERS:
                raise ValueError(
                    f"音频输出不支持容器 {container}，请使用 {sorted(AUDIO_CONTAINERS)}"
                )
            if self.subtitle_tracks:
                raise ValueError("音频输出不能包含字幕轨")
            if self.width or self.height or self.fps:
                raise ValueError("音频输出不能缩放或修改帧率")
            if self.hdr_tonemap or self.keep_10bit:
                raise ValueError("音频输出不支持 HDR/10bit 选项")
            for track in self.audio_tracks:
                if track.codec not in _CONTAINER_AUDIO[container]:
                    raise ValueError(
                        f"{container} 不支持音频编码 {track.codec}"
                    )
            return self

        if container in AUDIO_CONTAINERS:
            raise ValueError(
                f"视频输出不能使用音频容器 {container}；请设置 media_type='audio'"
            )
        if self.video_codec not in _CONTAINER_VIDEO[container]:
            raise ValueError(
                f"{container} 不支持视频编码 {self.video_codec}"
            )
        for track in self.audio_tracks:
            if track.codec not in _CONTAINER_AUDIO[container]:
                raise ValueError(
                    f"{container} 不支持音频编码 {track.codec}"
                )
        for track in self.subtitle_tracks:
            if track.codec not in _CONTAINER_SUBTITLE[container]:
                raise ValueError(
                    f"{container} 不支持字幕编码 {track.codec}"
                )
            if track.src is not None and track.codec == "copy":
                raise ValueError("外挂字幕不能使用 copy，必须指定目标字幕编码")

        if self.video_codec == "copy":
            if self.width or self.height:
                raise ValueError("视频 copy 模式不能缩放")
            if self.fps or self.fps_mode != "passthrough":
                raise ValueError("视频 copy 模式不能改帧率/帧率模式")
            if self.hdr_tonemap:
                raise ValueError("视频 copy 模式不能做 HDR tonemap")
            if self.keep_10bit:
                raise ValueError("视频 copy 模式无需 keep_10bit")

        if self.keep_10bit and self.video_codec == "libx264":
            raise ValueError("libx264 不支持通用 10bit 输出，请使用 libx265/libsvtav1/prores_ks")
        if self.keep_10bit and self.pix_fmt and "10" not in self.pix_fmt:
            raise ValueError("keep_10bit=true 时 pix_fmt 必须包含 10")
        if (self.width is None) != (self.height is None):
            raise ValueError("width 与 height 必须同时提供或同时为空")
        if self.width is not None and self.width % 2:
            raise ValueError("输出宽度必须为偶数")
        if self.height is not None and self.height % 2:
            raise ValueError("输出高度必须为偶数")
        return self

    def effective_pix_fmt(self) -> str | None:
        if self.pix_fmt:
            return self.pix_fmt
        if self.video_codec == "copy":
            return None
        if self.video_codec == "prores_ks":
            return "yuv422p10le" if self.keep_10bit else "yuv422p10le"
        if self.keep_10bit:
            return "yuv420p10le"
        return "yuv420p"


class TranscodeRequest(BaseModel):
    """一次转码请求；src=None 表示项目主素材。"""

    model_config = ConfigDict(extra="forbid")

    src: str | None = Field(default=None, description="工作区相对路径；None=项目主素材")
    dst: str | None = Field(default=None, description="工作区相对输出路径")
    profile: TranscodeProfile = Field(default_factory=TranscodeProfile)

    @model_validator(mode="after")
    def _dst_suffix(self) -> "TranscodeRequest":
        if self.dst and not self.dst.lower().endswith(f".{self.profile.container}"):
            raise ValueError(
                f"输出路径扩展名必须为 .{self.profile.container}"
            )
        return self


def container_extension(container: Container) -> str:
    """容器与输出扩展名一致（m4a/mp3/wav/flac 也直接使用容器名）。"""
    return container


def default_audio_codec(container: Container) -> AudioCodec:
    return DEFAULT_AUDIO_CODEC[container]


__all__ = [
    "AUDIO_CONTAINERS",
    "AudioCodec",
    "AudioOutput",
    "Container",
    "DEFAULT_AUDIO_CODEC",
    "FpsMode",
    "MediaType",
    "MetadataOptions",
    "ScaleMode",
    "SubtitleCodec",
    "SubtitleOutput",
    "TranscodeProfile",
    "TranscodeRequest",
    "VideoCodec",
    "container_extension",
    "default_audio_codec",
]
