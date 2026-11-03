from __future__ import annotations

import json
from pathlib import Path

import pytest

from smartcut.compiler import lower
from smartcut.contracts.effects import ColorEffect, FitEffect
from smartcut.contracts.edit_ir import AudioSpec, EditIR, ExportSpec, TimeRange, TrimOp
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.contracts.transcript import Transcript, TranscriptSegment, Word
from smartcut.errors import IRValidationError, UnsupportedFeature

GOLDEN = Path(__file__).parent / "golden" / "toolcalls_basic.json"
IR_GOLDEN = Path(__file__).parent / "golden" / "ir_basic.json"


def make_facts(duration: str = "10", fps: str = "30/1") -> MediaFacts:
    return MediaFacts(
        src="media/source.mp4",
        duration=RationalTime.from_seconds(duration, rate=90_000),
        fps=fps,
        video=StreamFacts(
            codec_type="video",
            width=320,
            height=240,
            avg_frame_rate=fps,
            r_frame_rate=fps,
        ),
        audio=StreamFacts(codec_type="audio", channels=2, sample_rate=48_000),
    )


def trim(start: str, end: str, tag: str | None = None) -> TrimOp:
    return TrimOp(
        src="media/source.mp4",
        range=TimeRange(
            start=RationalTime.from_seconds(start, rate=90_000),
            end=RationalTime.from_seconds(end, rate=90_000),
        ),
        tag=tag,
    )


def make_ir(sequence: list[TrimOp], **overrides) -> EditIR:
    facts = overrides.pop("media_facts", make_facts())
    return EditIR(
        project_id=overrides.pop("project_id", "p_test"),
        revision=overrides.pop("revision", 1),
        media_facts=facts,
        sequence=sequence,
        effects=overrides.pop("effects", []),
        audio=overrides.pop("audio", AudioSpec()),
        export=overrides.pop("export", ExportSpec()),
    )


def test_multi_segment_golden() -> None:
    plan = lower(make_ir([trim("0.5", "1.5", "A"), trim("3", "4", "B")]))
    payload = plan.dump()
    assert payload == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_ir_golden_roundtrip_and_lowering() -> None:
    ir = EditIR.model_validate_json(IR_GOLDEN.read_text(encoding="utf-8"))
    assert ir.project_id == "p_test"
    assert ir.media_facts.fps == "30/1"
    assert [op.tag for op in ir.sequence] == ["A", "B"]
    assert lower(ir).dump() == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_tool_sequence_and_paths() -> None:
    plan = lower(make_ir([trim("0.5", "1.5"), trim("3", "4")]))
    assert [call.tool for call in plan.tool_calls] == ["probe", "cut", "cut", "concat", "verify"]
    assert plan.output_path == "renders/r1/output.mp4"
    assert plan.tool_calls[1].args["dst"] == "renders/r1/seg_000.mp4"
    assert plan.tool_calls[3].args["inputs"] == [
        "renders/r1/seg_000.mp4",
        "renders/r1/seg_001.mp4",
    ]
    assert plan.expected_duration is not None
    assert plan.expected_duration.to_ffmpeg_seconds() == "2"


def test_single_segment_writes_output_directly() -> None:
    plan = lower(make_ir([trim("0.5", "1.5")]))
    assert [call.tool for call in plan.tool_calls] == ["probe", "cut", "verify"]
    assert plan.tool_calls[1].args["dst"] == "renders/r1/output.mp4"


def test_cut_normalizes_vfr_and_warns_10bit_hdr() -> None:
    facts = make_facts().model_copy(
        update={
            "vfr": True,
            "color_transfer": "smpte2084",
            "video": make_facts().video.model_copy(
                update={"pix_fmt": "yuv420p10le"}
            ),
        }
    )
    plan = lower(make_ir([trim("0.5", "1.5")], media_facts=facts))
    cut = next(call for call in plan.tool_calls if call.tool == "cut")
    assert cut.args["fps_mode"] == "cfr"
    assert any("VFR" in warning for warning in plan.warnings)
    assert any("8bit" in warning for warning in plan.warnings)
    assert any("偏色" in warning for warning in plan.warnings)


def test_copy_mode_is_lowered_explicitly() -> None:
    plan = lower(make_ir([trim("0.5", "1.5")], export=ExportSpec(cut_mode="copy")))
    assert plan.tool_calls[1].args["mode"] == "copy"


def test_color_effect_is_lowered() -> None:
    plan = lower(
        make_ir(
            [trim("0.5", "1.5")],
            effects=[ColorEffect(brightness=0.2, saturation=1.4)],
        )
    )
    post = next(call for call in plan.tool_calls if call.tool == "postprocess")
    assert post.args["video_filters_pre"] == [
        "eq=brightness=0.2:contrast=1:saturation=1.4:gamma=1"
    ]


def test_fit_effect_is_lowered() -> None:
    plan = lower(
        make_ir(
            [trim("0.5", "1.5")],
            effects=[FitEffect(anchor="center")],
        )
    )
    post_calls = [call for call in plan.tool_calls if call.tool == "postprocess"]
    assert len(post_calls) == 1
    assert post_calls[0].args["target_width"] == 1080
    assert post_calls[0].args["target_height"] == 1920
    assert "crop_width" in post_calls[0].args
    assert (plan.width, plan.height) == (1080, 1920)


def test_out_of_bounds_rejected() -> None:
    with pytest.raises(IRValidationError):
        lower(make_ir([trim("0.5", "11")]))


def test_platform_preset_rejected() -> None:
    with pytest.raises(UnsupportedFeature):
        lower(make_ir([trim("0.5", "1.5")], export=ExportSpec(preset="reels")))


def test_loudness_is_lowered() -> None:
    plan = lower(make_ir([trim("0.5", "1.5")], audio=AudioSpec(loudness="-14 LUFS")))
    post_calls = [call for call in plan.tool_calls if call.tool == "postprocess"]
    assert len(post_calls) == 1
    assert post_calls[0].args["target_lufs"] == -14.0


def test_invalid_loudness_rejected() -> None:
    with pytest.raises(UnsupportedFeature):
        lower(make_ir([trim("0.5", "1.5")], audio=AudioSpec(loudness="loud")))


def _transcript_with_hello() -> Transcript:
    word = Word(
        text="hello",
        start=RationalTime.from_seconds("0.6", rate=90_000),
        end=RationalTime.from_seconds("1.0", rate=90_000),
    )
    segment = TranscriptSegment(
        id=0,
        text="hello",
        start=word.start,
        end=word.end,
        words=[word],
    )
    return Transcript(language="en", words=[word], segments=[segment], degraded=False)


def test_p1_pipeline_order_with_fit_captions_loudness() -> None:
    plan = lower(
        make_ir(
            [trim("0.5", "1.5")],
            effects=[FitEffect(anchor="center")],
            audio=AudioSpec(loudness="-14 LUFS"),
            export=ExportSpec(burn_captions=True),
        ),
        transcript=_transcript_with_hello(),
    )
    assert [call.tool for call in plan.tool_calls] == [
        "probe",
        "cut",
        "postprocess",
        "verify",
    ]
    post = next(call for call in plan.tool_calls if call.tool == "postprocess")
    assert post.args["srt"] == "renders/r1/captions.srt"
    assert post.args["target_lufs"] == -14.0
    assert plan.caption_srt_path == "renders/r1/captions.srt"
    assert plan.caption_vtt_path == "renders/r1/captions.vtt"
    assert (plan.width, plan.height) == (1080, 1920)


def test_caption_style_is_mapped_to_postprocess() -> None:
    plan = lower(
        make_ir([trim("0.5", "1.5")], export=ExportSpec(burn_captions=True)),
        transcript=_transcript_with_hello(),
        caption_style={
            "font_size": 30,
            "color": "#112233",
            "position": "top",
        },
    )
    post = next(call for call in plan.tool_calls if call.tool == "postprocess")
    assert post.args["font_size"] == 30
    assert post.args["alignment"] == 8
    assert post.args["primary_colour"] == "&H00332211"


def test_burn_captions_without_transcript_warns() -> None:
    plan = lower(
        make_ir([trim("0.5", "1.5")], export=ExportSpec(burn_captions=True))
    )
    assert "postprocess" not in [call.tool for call in plan.tool_calls]
    assert any("字幕" in warning for warning in plan.warnings)


def test_unsafe_project_id_rejected_by_contract() -> None:
    with pytest.raises(Exception):
        make_ir([trim("0.5", "1.5")], project_id="../evil")
