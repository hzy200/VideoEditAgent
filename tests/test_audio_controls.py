from __future__ import annotations

import pytest

from smartcut.compiler import lower
from smartcut.contracts.edit_ir import AudioSpec, EditIR, TimeRange, TrimOp
from smartcut.contracts.time import RationalTime
from smartcut.runner.execute import ToolCallExecutor
from smartcut.runner.paths import Workspace
from smartcut.runner.tools.base import ToolContext
from smartcut.runner.tools.postprocess import PostProcessArgs, build_argv


def _trim(start: str, end: str) -> TrimOp:
    return TrimOp(
        src="media/source.mp4",
        range=TimeRange(
            start=RationalTime.from_seconds(start, rate=90_000),
            end=RationalTime.from_seconds(end, rate=90_000),
        ),
    )


def _ir(facts, audio: AudioSpec) -> EditIR:
    return EditIR(
        project_id="p_audio",
        revision=1,
        media_facts=facts,
        sequence=[_trim("0", facts.duration.to_ffmpeg_seconds())],
        audio=audio,
    )


def test_audio_adjustments_compile_to_single_postprocess(
    executor: ToolCallExecutor, ingested_source: str
) -> None:
    facts = executor.probe(ingested_source)
    ir = _ir(
        facts,
        AudioSpec(volume_db=3.5, pitch_semitones=5.0, loudness="-14 LUFS"),
    )
    plan = lower(ir)
    tools = [call.tool for call in plan.tool_calls]
    assert tools == ["probe", "cut", "postprocess", "verify"]
    post = next(call for call in plan.tool_calls if call.tool == "postprocess")
    assert post.args["volume_db"] == pytest.approx(3.5)
    assert post.args["pitch_semitones"] == pytest.approx(5.0)
    assert post.args["target_lufs"] == pytest.approx(-14.0)


def test_audio_adjustments_skip_without_audio(
    executor: ToolCallExecutor, ingested_source: str
) -> None:
    facts = executor.probe(ingested_source).model_copy(update={"audio": None})
    ir = _ir(facts, AudioSpec(volume_db=-6.0, pitch_semitones=-3.0))
    plan = lower(ir)
    assert any("没有音频轨" in warning for warning in plan.warnings)
    assert all(call.tool != "postprocess" for call in plan.tool_calls)


def test_postprocess_filter_order(settings, workspace: Workspace, ingested_source: str) -> None:
    argv = build_argv(
        PostProcessArgs(
            src=ingested_source,
            dst="renders/t/audio.mp4",
            volume_db=3.0,
            pitch_semitones=5.0,
            target_lufs=-14.0,
        ),
        ToolContext(settings=settings, workspace=workspace),
    )
    filters = argv[argv.index("-af") + 1].split(",")
    assert filters[0] == "aresample=48000"
    assert any(item.startswith("asetrate=") for item in filters)
    assert "volume=3.0dB" in filters
    assert filters[-1].startswith("loudnorm=")


def test_pitch_preserves_duration_e2e(
    executor: ToolCallExecutor, ingested_source: str, workspace: Workspace
) -> None:
    facts = executor.probe(ingested_source)
    ir = _ir(facts, AudioSpec(volume_db=-2.0, pitch_semitones=4.0))
    plan = lower(ir)
    for call in plan.tool_calls:
        executor.execute(call)
    output = executor.probe(plan.output_path)
    assert abs(output.duration.to_float() - 2.0) <= 0.08
    assert output.has_audio
