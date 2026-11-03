from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from smartcut.compiler import lower
from smartcut.contracts.audio import AudioAssetRef, AudioTrackSpec
from smartcut.contracts.edit_ir import AudioSpec, EditIR, TimeRange, TrimOp
from smartcut.contracts.time import RationalTime
from smartcut.errors import IRValidationError
from smartcut.runner.execute import ToolCallExecutor
from smartcut.runner.paths import Workspace
from smartcut.runner.tools.base import ToolContext
from smartcut.runner.tools.mix_audio import MixAudioArgs, build_argv


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _ir(facts, tracks=None) -> EditIR:
    return EditIR(
        project_id="p_tracks",
        revision=1,
        media_facts=facts,
        sequence=[
            TrimOp(
                src=facts.src,
                range=TimeRange(start=_rt("0"), end=_rt("2")),
            )
        ],
        audio=AudioSpec(tracks=tracks),
    )


def _track(track_id: str, asset_id: str, **overrides) -> AudioTrackSpec:
    return AudioTrackSpec(
        id=track_id,
        asset_id=asset_id,
        source_end=_rt("2") if asset_id == "main" else _rt("1"),
        **overrides,
    )


def _refs(facts) -> dict[str, AudioAssetRef]:
    return {
        "main": AudioAssetRef(
            id="main",
            name="原声",
            src=facts.src,
            duration=facts.duration,
        ),
        "a_bgm": AudioAssetRef(
            id="a_bgm",
            name="BGM",
            src="audio/bgm.wav",
            duration=_rt("1"),
        ),
    }


def test_tracks_compile_to_single_mix_tool(executor: ToolCallExecutor, ingested_source: str) -> None:
    facts = executor.probe(ingested_source)
    tracks = [
        _track("main", "main"),
        _track("bgm", "a_bgm", timeline_start=_rt("0.5"), gain_db=-6),
    ]
    plan = lower(_ir(facts, tracks), audio_assets=_refs(facts))
    assert [call.tool for call in plan.tool_calls] == [
        "probe",
        "cut",
        "mix_audio",
        "verify",
    ]
    mix = next(call for call in plan.tool_calls if call.tool == "mix_audio")
    assert len(mix.args["tracks"]) == 2
    assert mix.args["tracks"][1]["gain_db"] == -6
    assert mix.args["output_duration"]["value"] == 180_000
    assert plan.expect_audio is True


def test_solo_mute_and_explicit_silence(executor: ToolCallExecutor, ingested_source: str) -> None:
    facts = executor.probe(ingested_source)
    refs = _refs(facts)

    solo = [
        _track("main", "main"),
        _track("bgm", "a_bgm", solo=True),
    ]
    plan = lower(_ir(facts, solo), audio_assets=refs)
    mix = next(call for call in plan.tool_calls if call.tool == "mix_audio")
    assert [item["src"] for item in mix.args["tracks"]] == ["audio/bgm.wav"]

    muted = [_track("main", "main", muted=True)]
    plan = lower(_ir(facts, muted), audio_assets=refs)
    mix = next(call for call in plan.tool_calls if call.tool == "mix_audio")
    assert mix.args["tracks"] == []

    # 空列表表示明确静音；None 表示旧单音轨行为。
    plan = lower(_ir(facts, []), audio_assets=refs)
    assert "mix_audio" in [call.tool for call in plan.tool_calls]
    legacy = lower(_ir(facts, None), audio_assets=refs)
    assert "mix_audio" not in [call.tool for call in legacy.tool_calls]


def test_bgm_ducking_uses_sidechain(
    executor: ToolCallExecutor,
    ingested_source: str,
    settings,
    workspace: Workspace,
) -> None:
    facts = executor.probe(ingested_source)
    tracks = [
        _track("main", "main"),
        _track("bgm", "a_bgm", ducking=True, ducking_gain_db=-3),
    ]
    plan = lower(_ir(facts, tracks), audio_assets=_refs(facts))
    mix = next(call for call in plan.tool_calls if call.tool == "mix_audio")
    bgm = mix.args["tracks"][1]
    assert bgm["ducking"] is True
    assert bgm["ducking_source"]["src"] == facts.src

    (workspace.root / "audio").mkdir(parents=True, exist_ok=True)
    (workspace.root / "audio" / "bgm.wav").write_bytes(b"x")
    argv = build_argv(
        MixAudioArgs(
            src=ingested_source,
            dst="renders/mix.mp4",
            tracks=mix.args["tracks"],
            output_duration=_rt("2"),
        ),
        ToolContext(settings=settings, workspace=workspace),
    )
    joined = " ".join(argv)
    assert "sidechaincompress" in joined
    assert "volume=-3.0dB" in joined


def test_loop_track_fills_output_and_stream_loops(
    executor: ToolCallExecutor, ingested_source: str, settings, workspace: Workspace
) -> None:
    facts = executor.probe(ingested_source)
    tracks = [_track("bgm", "a_bgm", loop=True)]
    plan = lower(_ir(facts, tracks), audio_assets=_refs(facts))
    mix = next(call for call in plan.tool_calls if call.tool == "mix_audio")
    assert mix.args["tracks"][0]["loop"] is True
    end = mix.args["tracks"][0]["end"]
    assert end["value"] / end["rate"] == pytest.approx(2.0)

    argv = build_argv(
        MixAudioArgs(
            src=ingested_source,
            dst="renders/t/loop.mp4",
            tracks=[
                {
                    "src": ingested_source,
                    "start": _rt("0").model_dump(),
                    "end": _rt("2").model_dump(),
                    "loop": True,
                }
            ],
            output_duration=_rt("2"),
        ),
        ToolContext(settings=settings, workspace=workspace),
    )
    assert "-stream_loop" in argv
    assert argv[argv.index("-stream_loop") + 1] == "-1"


def test_missing_audio_asset_is_rejected(executor: ToolCallExecutor, ingested_source: str) -> None:
    facts = executor.probe(ingested_source)
    with pytest.raises(IRValidationError):
        lower(_ir(facts, [_track("bgm", "missing")]), audio_assets=_refs(facts))


def test_mix_argv_contains_track_and_master_filters(
    settings, workspace: Workspace, ingested_source: str
) -> None:
    argv = build_argv(
        MixAudioArgs(
            src=ingested_source,
            dst="renders/t/mix.mp4",
            tracks=[
                {
                    "src": ingested_source,
                    "start": _rt("0").model_dump(),
                    "end": _rt("2").model_dump(),
                    "timeline_start": _rt("0.5").model_dump(),
                    "gain_db": -3,
                    "fade_in": _rt("0.2").model_dump(),
                    "fade_out": _rt("0.2").model_dump(),
                }
            ],
            output_duration=_rt("2"),
            volume_db=1.5,
            pitch_semitones=2,
            target_lufs=-14,
        ),
        ToolContext(settings=settings, workspace=workspace),
    )
    graph = argv[argv.index("-filter_complex") + 1]
    assert "adelay=500:all=1" in graph
    assert "volume=-3.0dB" in graph
    assert "afade=t=in" in graph
    assert graph.endswith("[mix]")
    assert "volume=1.5dB" in graph
    assert "loudnorm=I=-14.0" in graph
    assert "-c:v" in argv and argv[argv.index("-c:v") + 1] == "copy"


def test_empty_tracks_render_silence(
    settings, workspace: Workspace, ingested_source: str
) -> None:
    argv = build_argv(
        MixAudioArgs(
            src=ingested_source,
            dst="renders/t/silence.mp4",
            tracks=[],
            output_duration=_rt("2"),
        ),
        ToolContext(settings=settings, workspace=workspace),
    )
    assert "anullsrc=r=48000:cl=stereo" in argv
    graph = argv[argv.index("-filter_complex") + 1]
    assert graph.startswith("[1:a]atrim=duration=2")
    assert graph.endswith("[mix]")


def test_mix_tracks_render_e2e(
    settings,
    executor: ToolCallExecutor,
    workspace: Workspace,
    ingested_source: str,
    ffmpeg_bin: str,
) -> None:
    bgm = workspace.audio_dir / "bgm.wav"
    subprocess.run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:duration=1",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(bgm),
        ],
        check=True,
        capture_output=True,
    )
    facts = executor.probe(ingested_source)
    refs = {
        "main": AudioAssetRef(
            id="main", src=facts.src, duration=facts.duration
        ),
        "a_bgm": AudioAssetRef(
            id="a_bgm", src="audio/bgm.wav", duration=_rt("1")
        ),
    }
    tracks = [
        _track("main", "main", gain_db=-2),
        _track("bgm", "a_bgm", timeline_start=_rt("0.5"), gain_db=-6),
    ]
    plan = lower(_ir(facts, tracks), audio_assets=refs)
    for call in plan.tool_calls:
        executor.execute(call)
    output = executor.probe(plan.output_path)
    assert abs(output.duration.to_float() - 2.0) <= 0.08
    assert output.has_audio
