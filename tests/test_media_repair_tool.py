from __future__ import annotations

from pathlib import Path

from smartcut.config import Settings
from smartcut.contracts.transcode import (
    AudioOutput,
    TranscodeProfile,
)
from smartcut.runner.paths import Workspace
from smartcut.runner.tools.base import ToolContext
from smartcut.runner.tools.media_repair import MediaRepairArgs, build_argv
from smartcut.runner.tools.transcode import TranscodeArgs
from smartcut.runner.tools.transcode import build_argv as transcode_argv


def _ctx(settings: Settings, workspace: Workspace) -> ToolContext:
    return ToolContext(settings=settings, workspace=workspace)


def _touch(workspace: Workspace, relative: str) -> None:
    path = workspace.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


def _video_profile(**overrides) -> TranscodeProfile:
    values = dict(
        video_codec="libx264",
        preset="veryfast",
        crf=23,
        fps="30",
        audio_tracks=[AudioOutput(codec="aac", bitrate="128k")],
    )
    values.update(overrides)
    return TranscodeProfile(**values)


def test_remux_uses_error_tolerant_copy(
    settings: Settings,
    workspace: Workspace,
) -> None:
    _touch(workspace, "media/a.mp4")
    args = MediaRepairArgs(
        src="media/a.mp4",
        dst="repairs/a.mp4",
        strategy="remux",
        profile=TranscodeProfile(video_codec="copy", fps_mode="passthrough"),
    )
    joined = " ".join(build_argv(args, _ctx(settings, workspace)))
    assert "-err_detect ignore_err" in joined
    assert "+genpts" in joined
    assert "-map 0:v:0?" in joined
    assert "-map 0:a:0?" in joined
    assert "-c copy" in joined
    assert "-avoid_negative_ts make_zero" in joined
    assert "-sn" in joined


def test_reencode_uses_cfr_and_resample(
    settings: Settings,
    workspace: Workspace,
) -> None:
    _touch(workspace, "media/a.mp4")
    args = MediaRepairArgs(
        src="media/a.mp4",
        dst="repairs/a.mp4",
        strategy="reencode",
        profile=_video_profile(),
    )
    joined = " ".join(build_argv(args, _ctx(settings, workspace)))
    assert "-vf fps=30,setpts=PTS-STARTPTS" in joined
    assert "-af aresample=async=1:first_pts=0" in joined
    assert "-c:v libx264" in joined
    assert "-fps_mode cfr" in joined
    assert "-c:a:0 aac" in joined


def test_audio_sync_delays_audio(
    settings: Settings,
    workspace: Workspace,
) -> None:
    _touch(workspace, "media/a.mp4")
    args = MediaRepairArgs(
        src="media/a.mp4",
        dst="repairs/a.mp4",
        strategy="audio_sync",
        profile=_video_profile(),
        audio_offset_seconds=0.5,
        pad_duration_seconds=2.0,
    )
    joined = " ".join(build_argv(args, _ctx(settings, workspace)))
    assert "-c:v copy" in joined
    assert "adelay=500:all=1" in joined
    assert "aresample=async=1:first_pts=0" in joined
    assert "apad=whole_dur=2.000" in joined
    assert "-shortest" in joined


def test_audio_sync_trims_early_audio(
    settings: Settings,
    workspace: Workspace,
) -> None:
    _touch(workspace, "media/a.mp4")
    args = MediaRepairArgs(
        src="media/a.mp4",
        dst="repairs/a.mp4",
        strategy="audio_sync",
        profile=_video_profile(),
        audio_offset_seconds=-0.25,
    )
    joined = " ".join(build_argv(args, _ctx(settings, workspace)))
    assert "atrim=start=0.250000,asetpts=PTS-STARTPTS" in joined
    assert "adelay" not in joined


def test_sharpen_adds_light_unsharp(
    settings: Settings,
    workspace: Workspace,
) -> None:
    _touch(workspace, "media/a.mp4")
    args = MediaRepairArgs(
        src="media/a.mp4",
        dst="repairs/a.mp4",
        strategy="sharpen",
        profile=_video_profile(),
        sharpen_strength=0.6,
    )
    joined = " ".join(build_argv(args, _ctx(settings, workspace)))
    assert "unsharp=5:5:0.60:5:5:0" in joined
    assert "-c:v libx264" in joined


def test_audio_only_repair_has_no_video_stream(
    settings: Settings,
    workspace: Workspace,
) -> None:
    _touch(workspace, "audio/a.wav")
    args = MediaRepairArgs(
        src="audio/a.wav",
        dst="repairs/a.m4a",
        strategy="reencode",
        has_video=False,
        profile=TranscodeProfile(
            media_type="audio",
            container="m4a",
            audio_tracks=[AudioOutput(codec="aac", bitrate="128k")],
        ),
    )
    joined = " ".join(build_argv(args, _ctx(settings, workspace)))
    assert "-vn" in joined
    assert "-map 0:a:0?" in joined
    assert "0:v:0" not in joined
    assert "-c:a:0 aac" in joined


def test_transcode_audio_only_maps_audio(
    settings: Settings,
    workspace: Workspace,
) -> None:
    _touch(workspace, "audio/a.wav")
    profile = TranscodeProfile(
        media_type="audio",
        container="mp3",
        audio_tracks=[AudioOutput(codec="libmp3lame", bitrate="192k")],
    )
    joined = " ".join(
        transcode_argv(
            TranscodeArgs(
                src="audio/a.wav",
                dst="exports/a.mp3",
                profile=profile,
            ),
            _ctx(settings, workspace),
        )
    )
    assert "-vn" in joined
    assert "-map 0:a:0?" in joined
    assert "0:v:0" not in joined
    assert "-c:a:0 libmp3lame" in joined
    assert "-b:a:0 192k" in joined
