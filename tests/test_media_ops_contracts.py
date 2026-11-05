from __future__ import annotations

import pytest
from pydantic import ValidationError

from smartcut.contracts.media_ops import (
    MediaDiagnosis,
    MediaGoalDraft,
    RepairRequest,
)
from smartcut.contracts.transcode import (
    AudioOutput,
    SubtitleOutput,
    TranscodeProfile,
    TranscodeRequest,
    default_audio_codec,
)


def test_audio_only_profile_accepts_audio_containers() -> None:
    for container in ("mp3", "m4a", "wav", "flac"):
        profile = TranscodeProfile(
            media_type="audio",
            container=container,
            audio_tracks=[
                AudioOutput(
                    codec=default_audio_codec(container),
                    bitrate="128k" if container in {"mp3", "m4a"} else None,
                )
            ],
        )
        assert profile.media_type == "audio"
        assert profile.container == container


def test_audio_only_profile_rejects_video_containers_and_options() -> None:
    with pytest.raises(ValidationError):
        TranscodeProfile(media_type="audio", container="mp4")
    with pytest.raises(ValidationError):
        TranscodeProfile(
            media_type="audio",
            container="m4a",
            width=1280,
            height=720,
        )
    with pytest.raises(ValidationError):
        TranscodeProfile(
            media_type="audio",
            container="m4a",
            subtitle_tracks=[SubtitleOutput(codec="mov_text")],
        )


def test_video_profile_rejects_audio_only_container() -> None:
    with pytest.raises(ValidationError):
        TranscodeProfile(media_type="video", container="mp3")


def test_transcode_request_audio_suffix() -> None:
    request = TranscodeRequest(
        profile=TranscodeProfile(media_type="audio", container="mp3"),
        dst="exports/a.mp3",
    )
    assert request.dst == "exports/a.mp3"
    with pytest.raises(ValidationError):
        TranscodeRequest(
            profile=TranscodeProfile(media_type="audio", container="mp3"),
            dst="exports/a.m4a",
        )


def test_media_goal_draft_rejects_paths_and_commands() -> None:
    draft = MediaGoalDraft(
        operation="compress",
        target_container="m4a",
        target_audio_codec="aac",
        crf=28,
        reason="压缩音频",
    )
    assert draft.target_container == "m4a"
    with pytest.raises(ValidationError):
        MediaGoalDraft(
            operation="repair",
            src="media/source.mp4",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        MediaGoalDraft(
            operation="repair",
            command="ffmpeg -i x",  # type: ignore[call-arg]
        )


def test_repair_request_defaults_and_bounds() -> None:
    request = RepairRequest()
    assert request.strategy == "auto"
    assert request.profile.video_codec == "libx264"
    assert request.profile.audio_tracks[0].codec == "aac"
    with pytest.raises(ValidationError):
        RepairRequest(audio_offset_seconds=999)
    with pytest.raises(ValidationError):
        RepairRequest(sharpen_strength=2.0)


def test_diagnosis_allows_missing_facts() -> None:
    diagnosis = MediaDiagnosis(src="media/missing.mp4", probe_ok=False)
    assert diagnosis.facts is None
    assert diagnosis.recommended == "auto"
