from __future__ import annotations

import pytest
from pydantic import ValidationError

from smartcut.compiler import lower
from smartcut.compiler.effects import effect_signature
from smartcut.contracts.effects import (
    BlurEffect,
    ColorEffect,
    FadeEffect,
    FitEffect,
    SharpenEffect,
    VignetteEffect,
    ZoomEffect,
)
from smartcut.contracts.edit_ir import (
    AudioSpec,
    EditIR,
    ExportSpec,
    TimeRange,
    TrimOp,
    assign_trim_ids,
    trim_id_for,
)
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.errors import IRValidationError, UnsupportedFeature
from smartcut.runner.tools.base import ToolContext
from smartcut.runner.tools.cut import CutArgs
from smartcut.runner.tools.postprocess import PostProcessArgs
from smartcut.runner.tools.postprocess import build_argv as postprocess_argv


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _facts(*, fps: str | None = "30/1", duration: str = "10") -> MediaFacts:
    return MediaFacts(
        src="media/a.mp4",
        duration=_rt(duration),
        fps=fps,
        video=StreamFacts(
            codec_type="video",
            width=1920,
            height=1080,
            avg_frame_rate=fps,
            r_frame_rate=fps,
        ),
        audio=StreamFacts(codec_type="audio", channels=2, sample_rate=48_000),
    )


def _trim(start: str = "0.5", end: str = "4.5") -> TrimOp:
    return TrimOp(src="media/a.mp4", range=TimeRange(start=_rt(start), end=_rt(end)))


def _ir(effects: list, *, facts: MediaFacts | None = None, export=None) -> EditIR:
    return EditIR(
        project_id="p_test",
        revision=1,
        media_facts=facts or _facts(),
        sequence=[_trim()],
        effects=effects,
        audio=AudioSpec(),
        export=export or ExportSpec(),
    )


def _post(plan):
    return next(call for call in plan.tool_calls if call.tool == "postprocess")


def test_no_effects_keeps_pipeline_without_video_filters() -> None:
    plan = lower(_ir([]))
    assert all(
        "video_filters_pre" not in call.args and "video_filters_post" not in call.args
        for call in plan.tool_calls
    )


def test_static_effects_lowered_in_fixed_order() -> None:
    plan = lower(
        _ir(
            [
                ColorEffect(brightness=0.2, contrast=1.1, saturation=1.2, gamma=1.0),
                BlurEffect(radius=3.0),
                SharpenEffect(amount=0.8),
                VignetteEffect(angle=0.4),
            ]
        )
    )
    post = _post(plan)
    assert post.args["video_filters_pre"] == [
        "eq=brightness=0.2:contrast=1.1:saturation=1.2:gamma=1",
        "gblur=sigma=3",
        "unsharp=5:5:0.8",
        "vignette=angle=0.4",
    ]
    assert "video_filters_post" not in post.args


def test_color_temperature_maps_to_kelvin() -> None:
    plan = lower(_ir([ColorEffect(temperature=50)]))
    assert "colortemperature=temperature=8000" in _post(plan).args[
        "video_filters_pre"
    ]


def test_fade_filters_use_output_duration() -> None:
    plan = lower(
        _ir([FadeEffect(fade_in=_rt("1"), fade_out=_rt("1.5"))])
    )
    assert _post(plan).args["video_filters_post"] == [
        "fade=t=in:st=0:d=1",
        "fade=t=out:st=2.5:d=1.5",
    ]


def test_zoom_filter_expression_and_requires_fps() -> None:
    plan = lower(_ir([ZoomEffect(start_scale=1.0, end_scale=1.2)]))
    zoom = _post(plan).args["video_filters_post"][0]
    assert zoom.startswith("zoompan=z=1+(1.2-1)*on/120:")
    assert "fps=30/1" in zoom

    with pytest.raises(UnsupportedFeature):
        lower(_ir([ZoomEffect(start_scale=1.0, end_scale=1.2)], facts=_facts(fps=None)))


def test_default_effects_produce_no_filter_but_warning() -> None:
    plan = lower(_ir([ColorEffect()]))
    assert all(call.tool != "postprocess" for call in plan.tool_calls)
    assert any("默认值" in warning for warning in plan.warnings)


def test_duplicate_effect_rejected() -> None:
    with pytest.raises(IRValidationError):
        lower(_ir([ColorEffect(brightness=0.1), ColorEffect(brightness=0.2)]))




def test_fade_longer_than_output_rejected() -> None:
    with pytest.raises(IRValidationError):
        lower(
            _ir(
                [FadeEffect(fade_in=_rt("2"), fade_out=_rt("2"))],
            ).model_copy(update={"sequence": [_trim("0.5", "1.5")]})
        )


def test_effect_signature_ignores_disabled() -> None:
    assert effect_signature([BlurEffect(radius=2.0, enabled=False)]) == []
    assert effect_signature([BlurEffect(radius=2.0)])[0]["radius"] == 2.0


def test_postprocess_rejects_injected_filter() -> None:
    with pytest.raises(ValidationError):
        PostProcessArgs(
            src="media/a.mp4",
            dst="renders/x.mp4",
            video_filters_pre=["eq=brightness=0.1; rm -rf /"],
        )


def test_cut_rejects_copy_with_filters() -> None:
    with pytest.raises(ValidationError):
        CutArgs(
            src="media/a.mp4",
            dst="renders/x.mp4",
            start=_rt("0"),
            end=_rt("1"),
            mode="copy",
            video_filters=["gblur=sigma=2"],
        )


def test_filters_are_placed_around_fit(settings, workspace, ingested_source) -> None:
    args = PostProcessArgs(
        src=ingested_source,
        dst="renders/t/effects.mp4",
        crop_x=0,
        crop_y=0,
        crop_width=608,
        crop_height=1080,
        target_width=1080,
        target_height=1920,
        video_filters_pre=["eq=brightness=0.2:contrast=1:saturation=1:gamma=1"],
        video_filters_post=["fade=t=in:st=0:d=0.5"],
    )
    argv = postprocess_argv(args, ToolContext(settings=settings, workspace=workspace))
    filters = argv[argv.index("-vf") + 1].split(",")
    assert filters[0].startswith("eq=")
    assert filters[1].startswith("crop=")
    assert filters[2].startswith("scale=")
    assert filters[3] == "setsar=1"
    assert filters[4].startswith("fade=")


def test_assign_trim_ids_is_stable_and_unique() -> None:
    first = assign_trim_ids([_trim()])
    second = assign_trim_ids([_trim()])
    assert first[0].id == second[0].id
    assert first[0].id is not None

    duplicated = [_trim(), _trim()]
    ids = [op.id for op in assign_trim_ids(duplicated)]
    assert len(ids) == len(set(ids))


def _segment_ir(
    effects: list,
    *,
    start: str = "0.5",
    end: str = "2.5",
    export=None,
) -> tuple[EditIR, str]:
    time_range = TimeRange(start=_rt(start), end=_rt(end))
    segment_id = trim_id_for("media/a.mp4", time_range)
    ir = EditIR(
        project_id="p_test",
        revision=1,
        media_facts=_facts(),
        sequence=[
            TrimOp(id=segment_id, src="media/a.mp4", range=time_range)
        ],
        effects=effects,
        audio=AudioSpec(),
        export=export or ExportSpec(),
    )
    return ir, segment_id


def test_segment_effects_are_lowered_into_cut() -> None:
    time_range = TimeRange(start=_rt("0.5"), end=_rt("2.5"))
    segment_id = trim_id_for("media/a.mp4", time_range)
    ir = EditIR(
        project_id="p_test",
        media_facts=_facts(),
        sequence=[TrimOp(id=segment_id, src="media/a.mp4", range=time_range)],
        effects=[
            ColorEffect(
                brightness=0.2, target="segment", segment_id=segment_id
            ),
            FadeEffect(
                fade_in=_rt("0.5"), target="segment", segment_id=segment_id
            ),
        ],
    )
    plan = lower(ir)
    cut = next(call for call in plan.tool_calls if call.tool == "cut")
    assert cut.args["video_filters"] == [
        "eq=brightness=0.2:contrast=1:saturation=1:gamma=1",
        "fade=t=in:st=0:d=0.5",
    ]
    assert all(call.tool != "postprocess" for call in plan.tool_calls)


def test_segment_effect_missing_target_rejected() -> None:
    ir, _segment_id = _segment_ir(
        [
            BlurEffect(
                radius=2.0,
                target="segment",
                segment_id="seg_missing",
            )
        ]
    )
    with pytest.raises(IRValidationError):
        lower(ir)


def test_segment_fade_cannot_exceed_segment_duration() -> None:
    time_range = TimeRange(start=_rt("0.5"), end=_rt("2.5"))
    segment_id = trim_id_for("media/a.mp4", time_range)
    ir = EditIR(
        project_id="p_test",
        media_facts=_facts(),
        sequence=[TrimOp(id=segment_id, src="media/a.mp4", range=time_range)],
        effects=[
            FadeEffect(
                fade_in=_rt("2"),
                fade_out=_rt("1"),
                target="segment",
                segment_id=segment_id,
            )
        ],
    )
    with pytest.raises(IRValidationError):
        lower(ir)


def test_copy_mode_with_segment_effects_rejected() -> None:
    time_range = TimeRange(start=_rt("0.5"), end=_rt("2.5"))
    segment_id = trim_id_for("media/a.mp4", time_range)
    ir = EditIR(
        project_id="p_test",
        media_facts=_facts(),
        sequence=[TrimOp(id=segment_id, src="media/a.mp4", range=time_range)],
        effects=[
            ColorEffect(target="segment", segment_id=segment_id)
        ],
        export=ExportSpec(cut_mode="copy"),
    )
    with pytest.raises(UnsupportedFeature):
        lower(ir)


def test_global_and_segment_effects_are_split() -> None:
    time_range = TimeRange(start=_rt("0.5"), end=_rt("2.5"))
    segment_id = trim_id_for("media/a.mp4", time_range)
    ir = EditIR(
        project_id="p_test",
        media_facts=_facts(),
        sequence=[TrimOp(id=segment_id, src="media/a.mp4", range=time_range)],
        effects=[
            ColorEffect(brightness=0.1),
            BlurEffect(
                radius=2.0, target="segment", segment_id=segment_id
            ),
        ],
    )
    plan = lower(ir)
    cut = next(call for call in plan.tool_calls if call.tool == "cut")
    post = next(call for call in plan.tool_calls if call.tool == "postprocess")
    assert cut.args["video_filters"] == ["gblur=sigma=2"]
    assert post.args["video_filters_pre"] == [
        "eq=brightness=0.1:contrast=1:saturation=1:gamma=1"
    ]


def test_edit_ir_rejects_duplicate_trim_ids() -> None:
    with pytest.raises(ValidationError):
        EditIR(
            project_id="p_test",
            media_facts=_facts(),
            sequence=[
                _trim().model_copy(update={"id": "seg_same"}),
                _trim().model_copy(update={"id": "seg_same"}),
            ],
        )
