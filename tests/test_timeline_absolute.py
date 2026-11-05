from __future__ import annotations

from pathlib import Path

from smartcut.compiler.lower import lower
from smartcut.config import Settings
from smartcut.contracts.edit_ir import (
    ClipTransform,
    EditIR,
    TimeRange,
    TrimOp,
)
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.time import RationalTime
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _services(settings: Settings) -> tuple[Services, JobService, Database]:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    return Services(settings, db, jobs, transcriber=NullTranscriber()), jobs, db


def test_absolute_timeline_track0_transition_with_overlay(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("absolute-transition")["id"]
        first = services.ingest(project_id, str(synthetic_video))
        second = services.add_media_asset(project_id, str(synthetic_video))
        primary = MediaFacts.model_validate(first["facts"])
        secondary = MediaFacts.model_validate(second["facts"])
        ir = EditIR(
            project_id=project_id,
            media_facts=primary,
            source_facts={second["src"]: secondary},
            sequence=[
                TrimOp(
                    id="clip_a",
                    src=first["src"],
                    range=TimeRange(start=_rt("0"), end=_rt("1")),
                    timeline_start=_rt("0"),
                ),
                TrimOp(
                    id="clip_b",
                    src=first["src"],
                    range=TimeRange(start=_rt("1"), end=_rt("2")),
                    timeline_start=_rt("0.5"),
                ),
                TrimOp(
                    id="overlay_c",
                    src=second["src"],
                    range=TimeRange(start=_rt("0"), end=_rt("0.5")),
                    timeline_start=_rt("0.25"),
                    track=1,
                    transform=ClipTransform(
                        x=0.65, y=0.05, width=0.3, height=0.3
                    ),
                ),
            ],
            transitions=[
                {
                    "after": "clip_a",
                    "type": "wipeleft",
                    "duration": _rt("0.5"),
                    "audio_curve": "log",
                }
            ],
        )
        plan = lower(ir)
        tools = [call.tool for call in plan.tool_calls]
        assert "compose" in tools
        assert "timeline_compose" in tools
        timeline_call = next(
            call for call in plan.tool_calls if call.tool == "timeline_compose"
        )
        assert timeline_call.args["base"]
        assert timeline_call.args["clips"][0]["track"] == 1
        submitted = services.submit_revision(project_id, ir)
        result = services.render(project_id, int(submitted["revision"]))
        assert Path(result["output_abs"]).is_file()
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_absolute_timeline_pip_keyframes_render(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("keyframes")["id"]
        first = services.ingest(project_id, str(synthetic_video))
        second = services.add_media_asset(project_id, str(synthetic_video))
        primary = MediaFacts.model_validate(first["facts"])
        secondary = MediaFacts.model_validate(second["facts"])
        ir = EditIR(
            project_id=project_id,
            media_facts=primary,
            source_facts={second["src"]: secondary},
            sequence=[
                TrimOp(
                    id="base",
                    src=first["src"],
                    range=TimeRange(start=_rt("0"), end=_rt("2")),
                    timeline_start=_rt("0"),
                ),
                TrimOp(
                    id="pip",
                    src=second["src"],
                    range=TimeRange(start=_rt("0"), end=_rt("2")),
                    timeline_start=_rt("0"),
                    track=1,
                    transform=ClipTransform(
                        x=0.6, y=0.05, width=0.3, height=0.3
                    ),
                    keyframes=[
                        {
                            "t": _rt("0"),
                            "x": 0.6,
                            "y": 0.05,
                            "width": 0.3,
                            "height": 0.3,
                            "opacity": 1.0,
                        },
                        {
                            "t": _rt("1"),
                            "x": 0.1,
                            "y": 0.4,
                            "width": 0.3,
                            "height": 0.3,
                            "opacity": 0.5,
                        },
                    ],
                ),
            ],
        )
        plan = lower(ir)
        call = next(
            call for call in plan.tool_calls if call.tool == "timeline_compose"
        )
        assert any(clip["keyframes"] for clip in call.args["clips"])
        submitted = services.submit_revision(project_id, ir)
        result = services.render(project_id, int(submitted["revision"]))
        assert Path(result["output_abs"]).is_file()
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_absolute_v2_transition_and_track_mute(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("v2-transition")["id"]
        first = services.ingest(project_id, str(synthetic_video))
        second = services.add_media_asset(project_id, str(synthetic_video))
        primary = MediaFacts.model_validate(first["facts"])
        secondary = MediaFacts.model_validate(second["facts"])
        ir = EditIR(
            project_id=project_id,
            media_facts=primary,
            source_facts={second["src"]: secondary},
            sequence=[
                TrimOp(
                    id="base",
                    src=first["src"],
                    range=TimeRange(start=_rt("0"), end=_rt("2")),
                    timeline_start=_rt("0"),
                ),
                TrimOp(
                    id="v2a",
                    src=second["src"],
                    range=TimeRange(start=_rt("0"), end=_rt("1")),
                    timeline_start=_rt("0"),
                    track=1,
                    transform=ClipTransform(
                        x=0.5, y=0.05, width=0.45, height=0.4
                    ),
                ),
                TrimOp(
                    id="v2b",
                    src=second["src"],
                    range=TimeRange(start=_rt("1"), end=_rt("2")),
                    timeline_start=_rt("0.5"),
                    track=1,
                    transform=ClipTransform(
                        x=0.05, y=0.05, width=0.45, height=0.4
                    ),
                ),
            ],
            transitions=[
                {
                    "after": "v2a",
                    "type": "fade",
                    "duration": _rt("0.5"),
                    "audio_curve": "tri",
                }
            ],
        )
        plan = lower(ir)
        call = next(
            call for call in plan.tool_calls if call.tool == "timeline_compose"
        )
        assert call.args["transitions"]
        submitted = services.submit_revision(project_id, ir)
        result = services.render(project_id, int(submitted["revision"]))
        assert Path(result["output_abs"]).is_file()

        muted = EditIR.model_validate(
            {
                **ir.model_dump(),
                "track_states": {"1": {"muted": True}},
            }
        )
        muted_plan = lower(muted)
        muted_call = next(
            call
            for call in muted_plan.tool_calls
            if call.tool == "timeline_compose"
        )
        assert all(clip["track"] == 0 for clip in muted_call.args["clips"])
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_absolute_timeline_gap_and_multi_track_render(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("absolute")["id"]
        first = services.ingest(project_id, str(synthetic_video))
        second = services.add_media_asset(project_id, str(synthetic_video))
        primary = MediaFacts.model_validate(first["facts"])
        secondary = MediaFacts.model_validate(second["facts"])
        ir = EditIR(
            project_id=project_id,
            media_facts=primary,
            source_facts={second["src"]: secondary},
            sequence=[
                TrimOp(
                    src=first["src"],
                    range=TimeRange(start=_rt("0"), end=_rt("1")),
                    timeline_start=_rt("0"),
                ),
                # 主轨 1s 空隙后再放第二段（1-2s），总时长约 3s。
                TrimOp(
                    src=first["src"],
                    range=TimeRange(start=_rt("1"), end=_rt("2")),
                    timeline_start=_rt("2"),
                ),
                # 叠加轨：0.5-1.5s 右上角 PiP。
                TrimOp(
                    src=second["src"],
                    range=TimeRange(start=_rt("0"), end=_rt("1")),
                    timeline_start=_rt("0.5"),
                    track=1,
                    transform=ClipTransform(
                        x=0.65, y=0.05, width=0.3, height=0.3
                    ),
                ),
            ],
        )
        plan = lower(ir)
        tools = [call.tool for call in plan.tool_calls]
        assert "timeline_compose" in tools
        assert "concat" not in tools
        compose = next(
            call for call in plan.tool_calls if call.tool == "timeline_compose"
        )
        assert compose.args["total_duration"]["value"] == 270_000
        assert compose.args["clips"][2]["track"] == 1
        assert compose.args["clips"][2]["x"] == 0.65

        submitted = services.submit_revision(project_id, ir)
        result = services.render(project_id, int(submitted["revision"]))
        workspace = services.workspaces.get(project_id)
        actual = services._executor(workspace).probe(result["output_path"])
        assert abs(actual.duration.to_float() - 3.0) < 0.2
        assert Path(result["output_abs"]).is_file()
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()
