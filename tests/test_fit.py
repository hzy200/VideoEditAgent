from __future__ import annotations

from smartcut.compiler import lower
from smartcut.compiler.fit import compute_fit_crop
from smartcut.contracts.effects import FitEffect
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.faces import FaceAnchor, FaceReport
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.runner.execute import ToolCallExecutor
from smartcut.runner.paths import Workspace


def _range(start: str, end: str) -> TimeRange:
    return TimeRange(
        start=RationalTime.from_seconds(start, rate=90_000),
        end=RationalTime.from_seconds(end, rate=90_000),
    )


def test_fit_crop_shifts_towards_face() -> None:
    faces = FaceReport(
        src="media/source.mp4",
        frame_width=320,
        frame_height=240,
        anchors=[
            FaceAnchor(
                t=RationalTime.from_seconds("0", rate=90_000),
                x=0.7,
                y=0.2,
                w=0.2,
                h=0.3,
            )
        ],
    )
    centered = compute_fit_crop(1920, 1080, None)
    shifted = compute_fit_crop(1920, 1080, faces)
    assert shifted.crop_x > centered.crop_x
    assert shifted.target_width == 1080
    assert shifted.target_height == 1920


def test_fit_tool_outputs_vertical_video(
    executor: ToolCallExecutor,
    workspace: Workspace,
    ingested_source: str,
) -> None:
    facts = executor.probe(ingested_source)
    ir = EditIR(
        project_id="p_fit",
        revision=1,
        media_facts=facts,
        sequence=[
            TrimOp(
                src=ingested_source,
                range=_range("0.2", "1.8"),
            )
        ],
        effects=[FitEffect(anchor="center")],
    )
    plan = lower(ir)
    for call in plan.tool_calls:
        executor.execute(call)

    output = executor.probe(plan.output_path)
    assert (output.width, output.height) == (1080, 1920)
    assert abs(output.duration.to_float() - 1.6) <= 0.2
    assert output.has_audio


def test_lower_uses_face_anchor_when_present() -> None:
    fps = "30/1"
    facts = MediaFacts(
        src="media/source.mp4",
        duration=RationalTime.from_seconds("10", rate=90_000),
        fps=fps,
        video=StreamFacts(
            codec_type="video",
            width=1920,
            height=1080,
            avg_frame_rate=fps,
            r_frame_rate=fps,
        ),
        audio=StreamFacts(codec_type="audio", channels=2),
    )
    faces = FaceReport(
        src=facts.src,
        frame_width=1920,
        frame_height=1080,
        anchors=[
            FaceAnchor(
                t=RationalTime.from_seconds("1", rate=90_000),
                x=0.7,
                y=0.2,
                w=0.2,
                h=0.3,
            )
        ],
    )
    ir = EditIR(
        project_id="p_fit",
        revision=1,
        media_facts=facts,
        sequence=[TrimOp(src=facts.src, range=_range("0", "1"))],
        effects=[FitEffect(anchor="face_bbox")],
    )
    plan = lower(ir, perception={"faces": faces.model_dump(mode="json")})
    post_call = next(call for call in plan.tool_calls if call.tool == "postprocess")
    centered = compute_fit_crop(1920, 1080, None)
    assert post_call.args["crop_x"] > centered.crop_x
