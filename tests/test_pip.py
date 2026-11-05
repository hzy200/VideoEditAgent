from __future__ import annotations

from pathlib import Path

from smartcut.compiler.lower import lower
from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.time import RationalTime
from smartcut.contracts.timeline_ops import PipClip
from smartcut.perception.transcribe import NullTranscriber
from smartcut.runner.paths import Workspace
from smartcut.runner.tools.base import ToolContext
from smartcut.runner.tools.pip_overlay import PipInput, PipOverlayArgs, build_argv
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _ctx(settings: Settings, workspace: Workspace) -> ToolContext:
    return ToolContext(settings=settings, workspace=workspace)


def _touch(workspace: Workspace, relative: str) -> None:
    path = workspace.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


def test_pip_overlay_argv(settings: Settings, workspace: Workspace) -> None:
    _touch(workspace, "media/base.mp4")
    _touch(workspace, "media/pip.mp4")
    args = PipOverlayArgs(
        src="media/base.mp4",
        dst="renders/pip.mp4",
        width=640,
        height=360,
        fps="30",
        pips=[
            PipInput(
                src="media/pip.mp4",
                source_start=_rt("0"),
                source_end=_rt("1"),
                timeline_start=_rt("0.5"),
                duration=_rt("1"),
                x=0.7,
                y=0.05,
                width=0.25,
                height=0.25,
            )
        ],
    )
    joined = " ".join(build_argv(args, _ctx(settings, workspace)))
    assert "pip.mp4" in joined
    assert "overlay=x=448:y=18" in joined
    assert "between(t,0.5,1.5)" in joined
    assert "colorchannelmixer=aa=1.000" in joined
    assert "-map [base1]" in joined


def test_compiler_emits_pip_overlay(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project_id = services.create_project("pip")["id"]
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
                    range=TimeRange(start=_rt("0"), end=_rt("2")),
                )
            ],
            pips=[
                PipClip(
                    id="pip_1",
                    src=second["src"],
                    source_start=_rt("0"),
                    source_end=_rt("1"),
                    timeline_start=_rt("0.5"),
                    duration=_rt("1"),
                )
            ],
        )
        plan = lower(ir)
        calls = [call for call in plan.tool_calls if call.tool == "pip_overlay"]
        assert len(calls) == 1
        assert calls[0].args["pips"][0]["src"] == second["src"]
        assert calls[0].args["width"] == primary.video.width

        submitted = services.submit_revision(project_id, ir)
        result = services.render(project_id, int(submitted["revision"]))
        assert Path(result["output_abs"]).is_file()
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()
