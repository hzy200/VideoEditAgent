"""媒体事实：ffprobe 的可序列化投影。

感知层只输出这些数值/时间戳，不写时间线、不调模型。
"""

from __future__ import annotations

from fractions import Fraction

from pydantic import BaseModel, ConfigDict, computed_field

from smartcut.contracts.time import RationalTime, fps_from_string


class StreamFacts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int = 0
    codec_type: str
    codec_name: str | None = None
    codec_long_name: str | None = None
    width: int | None = None
    height: int | None = None
    avg_frame_rate: str | None = None
    r_frame_rate: str | None = None
    pix_fmt: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    channel_layout: str | None = None
    duration: RationalTime | None = None
    start_time: RationalTime | None = None
    bit_rate: int | None = None
    nb_frames: int | None = None
    rotation: int = 0
    color_transfer: str | None = None
    time_base: str | None = None


class MediaFacts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    src: str
    format_name: str | None = None
    duration: RationalTime
    size_bytes: int | None = None
    video: StreamFacts | None = None
    audio: StreamFacts | None = None
    fps: str | None = None
    vfr: bool = False
    rotation: int = 0
    color_transfer: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_audio(self) -> bool:
        return self.audio is not None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def audio_channels(self) -> int:
        if self.audio is None:
            return 0
        return self.audio.channels or 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def width(self) -> int | None:
        return self.video.width if self.video is not None else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def height(self) -> int | None:
        return self.video.height if self.video is not None else None

    @property
    def fps_fraction(self) -> Fraction | None:
        if not self.fps:
            return None
        try:
            return fps_from_string(self.fps)
        except ValueError:
            return None
