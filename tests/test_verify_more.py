from __future__ import annotations

import pytest

from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.errors import VerificationError
from smartcut.runner.verify import VerifyArgs, verify_facts


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _facts(
    *,
    video_start: str | None = "0",
    audio_start: str | None = "0",
    video_duration: str = "2",
    audio_duration: str = "2",
    video_codec: str = "h264",
    audio_codec: str = "aac",
    pix_fmt: str = "yuv420p",
    frames: int = 60,
) -> MediaFacts:
    return MediaFacts(
        src="renders/x.mp4",
        duration=_rt("2"),
        fps="30/1",
        video=StreamFacts(
            codec_type="video",
            width=320,
            height=240,
            codec_name=video_codec,
            pix_fmt=pix_fmt,
            nb_frames=frames,
            duration=_rt(video_duration),
            start_time=_rt(video_start) if video_start is not None else None,
        ),
        audio=StreamFacts(
            codec_type="audio",
            codec_name=audio_codec,
            duration=_rt(audio_duration),
            start_time=_rt(audio_start) if audio_start is not None else None,
        ),
    )


def _args(**overrides) -> VerifyArgs:
    payload = {"src": "x.mp4", "expected_duration": _rt("2")}
    payload.update(overrides)
    return VerifyArgs(**payload)


def test_verify_checks_codec_and_pixel_format() -> None:
    verify_facts(
        _facts(),
        _args(
            expected_video_codec="h264",
            expected_audio_codec="aac",
            expected_pix_fmt="yuv420p",
        ),
    )
    with pytest.raises(VerificationError):
        verify_facts(_facts(video_codec="hevc"), _args(expected_video_codec="h264"))
    with pytest.raises(VerificationError):
        verify_facts(_facts(pix_fmt="yuv420p10le"), _args(expected_pix_fmt="yuv420p"))


def test_verify_checks_frame_count_with_tolerance() -> None:
    verify_facts(_facts(frames=61), _args(expected_frame_count=60))
    with pytest.raises(VerificationError):
        verify_facts(_facts(frames=70), _args(expected_frame_count=60))


def test_verify_rejects_av_start_offset() -> None:
    verify_facts(_facts(video_start="0.1", audio_start="0.2"), _args())
    with pytest.raises(VerificationError):
        verify_facts(
            _facts(video_start="0", audio_start="1.2"),
            _args(max_av_sync_seconds=0.5),
        )


def test_verify_rejects_av_duration_mismatch() -> None:
    verify_facts(
        _facts(video_duration="2", audio_duration="2.2"),
        _args(max_av_sync_seconds=0.5),
    )
    with pytest.raises(VerificationError):
        verify_facts(
            _facts(video_duration="2", audio_duration="3"),
            _args(max_av_sync_seconds=0.5),
        )


def test_verify_can_disable_av_sync_check() -> None:
    verify_facts(
        _facts(video_start="0", audio_start="3"),
        _args(check_av_sync=False),
    )
