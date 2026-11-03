"""成片事实校验：ffprobe 实际值 vs 编译器期望值。"""

from __future__ import annotations

from fractions import Fraction

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.media import MediaFacts
from smartcut.contracts.time import RationalTime, fps_from_string
from smartcut.errors import VerificationError


class VerifyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    expected_duration: RationalTime
    expected_fps: str | None = None
    expect_audio: bool = True
    expected_width: int | None = None
    expected_height: int | None = None
    expected_video_codec: str | None = None
    expected_audio_codec: str | None = None
    expected_pix_fmt: str | None = None
    expected_frame_count: int | None = Field(default=None, ge=0)
    check_av_sync: bool = True
    max_av_sync_seconds: float = Field(default=0.5, ge=0.0, le=10.0)
    tolerance_frames: int = Field(default=2, ge=0, le=30)


def verify_facts(actual: MediaFacts, expected: VerifyArgs) -> None:
    fps: Fraction | None = None
    if expected.expected_fps:
        try:
            fps = fps_from_string(expected.expected_fps)
        except ValueError:
            fps = None

    frame = Fraction(1, 1) / fps if fps else Fraction(1, 30)
    tolerance = max(frame * expected.tolerance_frames, Fraction(1, 10))
    diff = abs(actual.duration.as_fraction() - expected.expected_duration.as_fraction())
    if diff > tolerance:
        raise VerificationError(
            "时长不符: "
            f"expected={expected.expected_duration.to_ffmpeg_seconds()}s "
            f"actual={actual.duration.to_ffmpeg_seconds()}s "
            f"diff={float(diff):.3f}s tolerance={float(tolerance):.3f}s"
        )

    if expected.expect_audio and not actual.has_audio:
        raise VerificationError("期望含音频轨，但成片没有音频流")

    if fps is not None and actual.fps:
        try:
            actual_fps = fps_from_string(actual.fps)
        except ValueError:
            actual_fps = None
        if actual_fps is not None and abs(actual_fps - fps) > Fraction(1, 100):
            raise VerificationError(f"帧率不符: expected={fps} actual={actual_fps}")

    if expected.expected_width is not None and actual.width != expected.expected_width:
        raise VerificationError(
            f"宽度不符: expected={expected.expected_width} actual={actual.width}"
        )
    if expected.expected_height is not None and actual.height != expected.expected_height:
        raise VerificationError(
            f"高度不符: expected={expected.expected_height} actual={actual.height}"
        )

    if expected.expected_video_codec is not None:
        actual_codec = (actual.video.codec_name if actual.video else None) or ""
        if actual_codec.lower() != expected.expected_video_codec.lower():
            raise VerificationError(
                "视频编码不符: "
                f"expected={expected.expected_video_codec} actual={actual_codec or 'none'}"
            )

    if expected.expected_audio_codec is not None:
        actual_codec = (actual.audio.codec_name if actual.audio else None) or ""
        if actual_codec.lower() != expected.expected_audio_codec.lower():
            raise VerificationError(
                "音频编码不符: "
                f"expected={expected.expected_audio_codec} actual={actual_codec or 'none'}"
            )

    if expected.expected_pix_fmt is not None:
        actual_pix_fmt = (actual.video.pix_fmt if actual.video else None) or ""
        if actual_pix_fmt.lower() != expected.expected_pix_fmt.lower():
            raise VerificationError(
                "像素格式不符: "
                f"expected={expected.expected_pix_fmt} actual={actual_pix_fmt or 'none'}"
            )

    if (
        expected.expected_frame_count is not None
        and actual.video is not None
        and actual.video.nb_frames is not None
    ):
        frame_tolerance = max(
            expected.tolerance_frames,
            int(expected.expected_frame_count * 0.01) + 1,
        )
        frame_diff = abs(actual.video.nb_frames - expected.expected_frame_count)
        if frame_diff > frame_tolerance:
            raise VerificationError(
                "帧数不符: "
                f"expected={expected.expected_frame_count} "
                f"actual={actual.video.nb_frames} "
                f"diff={frame_diff} tolerance={frame_tolerance}"
            )

    if expected.check_av_sync and actual.video is not None and actual.audio is not None:
        tolerance = Fraction(str(expected.max_av_sync_seconds))
        video_start = (
            actual.video.start_time.as_fraction()
            if actual.video.start_time is not None
            else None
        )
        audio_start = (
            actual.audio.start_time.as_fraction()
            if actual.audio.start_time is not None
            else None
        )
        if video_start is not None and audio_start is not None:
            start_diff = abs(video_start - audio_start)
            if start_diff > tolerance:
                raise VerificationError(
                    "音视频起始偏移: "
                    f"video={float(video_start):.3f}s audio={float(audio_start):.3f}s "
                    f"diff={float(start_diff):.3f}s tolerance={float(tolerance):.3f}s"
                )
        for label, start in (("video", video_start), ("audio", audio_start)):
            if start is not None and abs(start) > tolerance:
                raise VerificationError(
                    f"{label} 起始时间偏移过大: {float(start):.3f}s"
                    f" tolerance={float(tolerance):.3f}s"
                )
        if actual.video.duration is not None and actual.audio.duration is not None:
            duration_diff = abs(
                actual.video.duration.as_fraction()
                - actual.audio.duration.as_fraction()
            )
            if duration_diff > tolerance:
                raise VerificationError(
                    "音视频时长不一致: "
                    f"video={actual.video.duration.to_ffmpeg_seconds()}s "
                    f"audio={actual.audio.duration.to_ffmpeg_seconds()}s "
                    f"diff={float(duration_diff):.3f}s "
                    f"tolerance={float(tolerance):.3f}s"
                )
