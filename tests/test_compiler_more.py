from __future__ import annotations

import pytest

from smartcut.compiler import lower
from smartcut.compiler.captions import format_srt, format_vtt
from smartcut.compiler.encoding import video_encoder_args
from smartcut.contracts.audio import AudioAssetRef, AudioTrackSpec
from smartcut.contracts.captions import CaptionCue
from smartcut.contracts.effects import FitEffect
from smartcut.contracts.edit_ir import (
    AudioSpec,
    EditIR,
    ExportSpec,
    TimeRange,
    TrimOp,
)
from smartcut.contracts.encoder import EncoderProfile
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.runner.tools.base import ToolContext
from smartcut.runner.tools.mix_audio import MixAudioArgs, build_argv as mix_argv
from smartcut.runner.tools.postprocess import PostProcessArgs, build_argv as post_argv


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _facts() -> MediaFacts:
    return MediaFacts(
        src="media/source.mp4",
        duration=_rt("10"),
        fps="30/1",
        video=StreamFacts(codec_type="video", width=640, height=360),
        audio=StreamFacts(codec_type="audio", channels=2, sample_rate=48_000),
    )


def _ir(**overrides) -> EditIR:
    facts = _facts()
    return EditIR(
        project_id="p_test",
        revision=1,
        media_facts=facts,
        sequence=[
            TrimOp(
                src=facts.src,
                range=TimeRange(start=_rt("0"), end=_rt("5")),
            )
        ],
        **overrides,
    )


def _refs(facts: MediaFacts) -> dict[str, AudioAssetRef]:
    return {
        "main": AudioAssetRef(
            id="main", src=facts.src, duration=facts.duration
        ),
        "a_bgm": AudioAssetRef(
            id="a_bgm", src="audio/bgm.wav", duration=_rt("3")
        ),
    }


def test_fit_subtitles_audio_merge_into_one_postprocess() -> None:
    ir = _ir(
        effects=[FitEffect(anchor="center")],
        export=ExportSpec(burn_captions=True),
        audio=AudioSpec(volume_db=2.0, pitch_semitones=1.0),
        subtitles=[
            CaptionCue(start=_rt("0"), end=_rt("2"), text="你好 SmartCut")
        ],
    )
    plan = lower(ir)
    assert [call.tool for call in plan.tool_calls] == [
        "probe",
        "cut",
        "postprocess",
        "verify",
    ]
    post = next(call for call in plan.tool_calls if call.tool == "postprocess")
    assert post.args["srt"].endswith("captions.srt")
    assert post.args["volume_db"] == 2.0
    assert post.args["pitch_semitones"] == 1.0
    assert "fit" in post.description and "subtitles" in post.description


def test_visual_postprocess_runs_before_multitrack_mix() -> None:
    facts = _facts()
    tracks = [
        AudioTrackSpec(
            id="main",
            role="main",
            asset_id="main",
            source_end=_rt("5"),
        )
    ]
    ir = _ir(
        effects=[FitEffect(anchor="center")],
        audio=AudioSpec(tracks=tracks),
    )
    plan = lower(ir, audio_assets=_refs(facts))
    assert [call.tool for call in plan.tool_calls] == [
        "probe",
        "cut",
        "postprocess",
        "mix_audio",
        "verify",
    ]
    post = next(call for call in plan.tool_calls if call.tool == "postprocess")
    mix = next(call for call in plan.tool_calls if call.tool == "mix_audio")
    assert post.args["dst"].endswith("visual.mp4")
    assert mix.args["src"] == post.args["dst"]
    assert plan.expect_audio is True


def test_postprocess_without_filters_copies_streams(
    settings, workspace, ingested_source: str
) -> None:
    argv = post_argv(
        PostProcessArgs(src=ingested_source, dst="renders/x.mp4"),
        ToolContext(settings=settings, workspace=workspace),
    )
    assert argv[argv.index("-c:v") + 1] == "copy"
    assert argv[argv.index("-c:a") + 1] == "copy"
    assert "-vf" not in argv
    assert "-af" not in argv


def test_mix_argv_two_tracks_amix_and_loop(
    settings, workspace, ingested_source: str
) -> None:
    bgm = workspace.audio_dir / "bgm.wav"
    bgm.write_bytes(b"stub")
    argv = mix_argv(
        MixAudioArgs(
            src=ingested_source,
            dst="renders/mix.mp4",
            tracks=[
                {
                    "src": ingested_source,
                    "start": _rt("0").model_dump(),
                    "end": _rt("5").model_dump(),
                    "gain_db": -3,
                },
                {
                    "src": "audio/bgm.wav",
                    "start": _rt("0").model_dump(),
                    "end": _rt("5").model_dump(),
                    "loop": True,
                    "timeline_start": _rt("1").model_dump(),
                },
            ],
            output_duration=_rt("5"),
            target_lufs=-14,
        ),
        ToolContext(settings=settings, workspace=workspace),
    )
    graph = argv[argv.index("-filter_complex") + 1]
    assert "amix=inputs=2:duration=longest:normalize=0" in graph
    assert "adelay=1000:all=1" in graph
    assert argv.count("-stream_loop") == 1
    assert "-t" in argv and argv[argv.index("-t") + 1] == "5"


def test_caption_formats_unicode_and_vtt_header() -> None:
    cues = [
        CaptionCue(start=_rt("0"), end=_rt("1.5"), text="中文字幕"),
        CaptionCue(start=_rt("2"), end=_rt("3"), text="second"),
    ]
    srt = format_srt(cues)
    vtt = format_vtt(cues)
    assert "中文字幕" in srt and "second" in srt
    assert "00:00:00,000 --> 00:00:01,500" in srt
    assert vtt.startswith("WEBVTT")
    assert "00:00:02.000 --> 00:00:03.000" in vtt


@pytest.mark.parametrize("codec,marker", [
    ("libx264", "-crf"),
    ("h264_nvenc", "-cq"),
    ("h264_qsv", "-global_quality"),
    ("h264_amf", "-qp_i"),
])
def test_encoder_args_cover_all_profiles(codec: str, marker: str) -> None:
    argv = video_encoder_args(EncoderProfile(codec=codec, quality=23))
    assert codec in argv
    assert marker in argv


def test_multitrack_truncates_and_warns_beyond_output() -> None:
    facts = _facts()
    tracks = [
        AudioTrackSpec(
            id="main",
            role="main",
            asset_id="main",
            source_end=_rt("5"),
        ),
        AudioTrackSpec(
            id="bgm",
            role="music",
            asset_id="a_bgm",
            source_end=_rt("3"),
            timeline_start=_rt("4"),
        ),
    ]
    plan = lower(_ir(audio=AudioSpec(tracks=tracks)), audio_assets=_refs(facts))
    assert any("超出成片时长" in warning for warning in plan.warnings)
    mix = next(call for call in plan.tool_calls if call.tool == "mix_audio")
    bgm = mix.args["tracks"][1]
    assert bgm["end"]["value"] / bgm["end"]["rate"] == pytest.approx(1.0)
