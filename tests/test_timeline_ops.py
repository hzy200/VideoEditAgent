from __future__ import annotations

import pytest

from smartcut.compiler import lower
from smartcut.compiler.captions import build_cues
from smartcut.contracts.edit_ir import EditIR, ExportSpec, TimeRange, TrimOp, trim_id_for
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.contracts.timeline_ops import TransitionSpec
from smartcut.contracts.tools import ToolCall
from smartcut.contracts.transcript import Transcript, TranscriptSegment, Word
from smartcut.errors import IRValidationError, UnsupportedFeature
from smartcut.runner.tools.base import ToolContext
from smartcut.runner.tools.compose import ComposeArgs, ComposeTransition
from smartcut.runner.tools.compose import build_argv as compose_argv
from smartcut.runner.tools.cut import CutArgs
from smartcut.runner.tools.cut import build_argv as cut_argv
from smartcut.runner.tools.postprocess import (
    OverlayInput,
    PostProcessArgs,
    RemovalInput,
)
from smartcut.runner.tools.postprocess import build_argv as post_argv


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _facts(*, duration: str = "10", has_audio: bool = True) -> MediaFacts:
    return MediaFacts(
        src="media/a.mp4",
        duration=_rt(duration),
        fps="30/1",
        video=StreamFacts(
            codec_type="video", width=1280, height=720, avg_frame_rate="30/1"
        ),
        audio=(
            StreamFacts(codec_type="audio", channels=2, sample_rate=48_000)
            if has_audio
            else None
        ),
    )


def _segments() -> tuple[TrimOp, TrimOp, str, str]:
    first_range = TimeRange(start=_rt("0"), end=_rt("2"))
    second_range = TimeRange(start=_rt("2"), end=_rt("4"))
    first_id = trim_id_for("media/a.mp4", first_range)
    second_id = trim_id_for("media/a.mp4", second_range)
    return (
        TrimOp(id=first_id, src="media/a.mp4", range=first_range),
        TrimOp(id=second_id, src="media/a.mp4", range=second_range),
        first_id,
        second_id,
    )


def test_total_duration_with_speed_and_transition() -> None:
    first, second, first_id, _second_id = _segments()
    first = first.model_copy(update={"speed": 2.0})
    ir = EditIR(
        project_id="p",
        media_facts=_facts(),
        sequence=[first, second],
        transitions=[TransitionSpec(after=first_id, duration=_rt("0.5"))],
    )
    assert ir.total_duration().to_ffmpeg_seconds() == "2.5"


def test_copy_mode_verify_tolerance_is_wider() -> None:
    first, second, _first_id, _second_id = _segments()
    copy_plan = lower(
        EditIR(
            project_id="p",
            media_facts=_facts(),
            sequence=[first, second],
            export=ExportSpec(cut_mode="copy"),
        )
    )
    copy_verify = next(
        call for call in copy_plan.tool_calls if call.tool == "verify"
    )
    assert copy_verify.args["tolerance_frames"] == 5

    reencode_plan = lower(
        EditIR(
            project_id="p",
            media_facts=_facts(),
            sequence=[first, second],
            export=ExportSpec(cut_mode="reencode"),
        )
    )
    reencode_verify = next(
        call for call in reencode_plan.tool_calls if call.tool == "verify"
    )
    assert reencode_verify.args["tolerance_frames"] == 2


def test_transition_validation_rejects_bad_target_and_copy() -> None:
    first, second, first_id, _second_id = _segments()
    with pytest.raises(IRValidationError):
        lower(
            EditIR(
                project_id="p",
                media_facts=_facts(),
                sequence=[first, second],
                transitions=[
                    TransitionSpec(after="seg_missing", duration=_rt("0.2"))
                ],
            )
        )
    with pytest.raises(UnsupportedFeature):
        lower(
            EditIR(
                project_id="p",
                media_facts=_facts(),
                sequence=[first, second],
                transitions=[
                    TransitionSpec(after=first_id, duration=_rt("0.2"))
                ],
                export=ExportSpec(cut_mode="copy"),
            )
        )


def test_cut_speed_argv_uses_setpts_and_atempo(
    settings, workspace, ingested_source
) -> None:
    argv = cut_argv(
        CutArgs(
            src=ingested_source,
            dst="renders/t/speed.mp4",
            start=_rt("0"),
            end=_rt("2"),
            speed=2.0,
            fps_mode="cfr",
        ),
        ToolContext(settings=settings, workspace=workspace),
    )
    assert argv.index("-t") < argv.index("-i")
    assert "setpts=PTS/2.0" in argv[argv.index("-vf") + 1]
    assert "atempo=2.0" in argv[argv.index("-af") + 1]


def test_compose_argv_builds_xfade_and_acrossfade(
    settings, workspace, ingested_source
) -> None:
    argv = compose_argv(
        ComposeArgs(
            inputs=[ingested_source, ingested_source],
            durations=[_rt("1"), _rt("2")],
            transitions=[ComposeTransition(duration=_rt("0.5"))],
            dst="renders/t/out.mp4",
            fps="30/1",
            fps_mode="cfr",
        ),
        ToolContext(settings=settings, workspace=workspace),
    )
    graph = argv[argv.index("-filter_complex") + 1]
    assert "xfade=transition=fade:duration=0.5:offset=0.5" in graph
    assert "acrossfade=d=0.5" in graph
    assert "[v1]" in argv
    assert "[a1]" in argv


def test_overlay_builds_drawtext(settings, workspace, ingested_source) -> None:
    argv = post_argv(
        PostProcessArgs(
            src=ingested_source,
            dst="renders/t/overlay.mp4",
            overlays=[
                OverlayInput(
                    text="Hello: world",
                    position="top_left",
                    start=_rt("0.5"),
                    end=_rt("2"),
                )
            ],
        ),
        ToolContext(settings=settings, workspace=workspace),
    )
    graph = argv[argv.index("-vf") + 1]
    assert graph.startswith("drawtext=")
    assert "text='Hello\\: world'" in graph
    assert "enable='between(t,0.5,2)'" in graph


def test_image_overlay_and_delogo_build_filter_complex(
    settings, workspace, ingested_source
) -> None:
    argv = post_argv(
        PostProcessArgs(
            src=ingested_source,
            dst="renders/t/watermark.mp4",
            overlays=[
                OverlayInput(
                    kind="image",
                    src=ingested_source,
                    width=80,
                    position="bottom_right",
                )
            ],
            removals=[
                RemovalInput(x=10, y=10, width=40, height=20)
            ],
        ),
        ToolContext(settings=settings, workspace=workspace),
    )
    assert "-filter_complex" in argv
    graph = argv[argv.index("-filter_complex") + 1]
    assert "delogo=x=10:y=10:w=40:h=20:show=0" in graph
    assert "[1:v]scale=80:-2" in graph
    assert "overlay=x=W-w-12:y=H-h-12" in graph
    assert "[vimg0]" in argv


def test_captions_remap_speed_and_transition() -> None:
    first, second, first_id, _second_id = _segments()
    first = first.model_copy(update={"speed": 2.0})
    ir = EditIR(
        project_id="p",
        media_facts=_facts(),
        sequence=[first, second],
        transitions=[TransitionSpec(after=first_id, duration=_rt("0.5"))],
    )
    word = Word(text="hello", start=_rt("2.5"), end=_rt("3.0"))
    transcript = Transcript(
        language="en",
        words=[word],
        segments=[
            TranscriptSegment(
                id=0, text="hello", start=word.start, end=word.end, words=[word]
            )
        ],
        degraded=False,
    )
    cues = build_cues(ir, transcript)
    assert len(cues) == 1
    # seg0 输出 1s，转场重叠 0.5s，因此 seg1 从 0.5s 开始；
    # 源 2.5s 距 seg1 起点 0.5s，speed=1 → 输出 1.0s。
    assert cues[0].start.to_ffmpeg_seconds() == "1"
    assert cues[0].end.to_ffmpeg_seconds() == "1.5"


def test_compose_render_e2e(executor, ingested_source) -> None:
    facts = executor.probe(ingested_source)
    executor.execute(
        ToolCall(
            seq=0,
            tool="cut",
            args={
                "src": ingested_source,
                "dst": "renders/t/seg0.mp4",
                "start": _rt("0").model_dump(),
                "end": _rt("0.8").model_dump(),
                "fps_mode": "cfr",
                "has_audio": facts.has_audio,
            },
        )
    )
    executor.execute(
        ToolCall(
            seq=1,
            tool="cut",
            args={
                "src": ingested_source,
                "dst": "renders/t/seg1.mp4",
                "start": _rt("1").model_dump(),
                "end": _rt("1.8").model_dump(),
                "fps_mode": "cfr",
                "has_audio": facts.has_audio,
            },
        )
    )
    executor.execute(
        ToolCall(
            seq=2,
            tool="compose",
            args={
                "inputs": ["renders/t/seg0.mp4", "renders/t/seg1.mp4"],
                "durations": [
                    _rt("0.8").model_dump(),
                    _rt("0.8").model_dump(),
                ],
                "transitions": [
                    {
                        "type": "fade",
                        "duration": _rt("0.2").model_dump(),
                    }
                ],
                "dst": "renders/t/composed.mp4",
                "has_audio": facts.has_audio,
                "fps": facts.fps,
                "fps_mode": "cfr",
            },
        )
    )
    result = executor.probe("renders/t/composed.mp4")
    assert 1.3 <= result.duration.to_float() <= 1.5


def test_speed_cut_render_e2e(executor, ingested_source) -> None:
    facts = executor.probe(ingested_source)
    executor.execute(
        ToolCall(
            seq=0,
            tool="cut",
            args={
                "src": ingested_source,
                "dst": "renders/t/fast.mp4",
                "start": _rt("0").model_dump(),
                "end": _rt("1").model_dump(),
                "speed": 2.0,
                "fps_mode": "cfr",
                "has_audio": facts.has_audio,
            },
        )
    )
    result = executor.probe("renders/t/fast.mp4")
    assert 0.4 <= result.duration.to_float() <= 0.6
