from __future__ import annotations

from pathlib import Path

from smartcut.compiler.lower import lower
from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
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


def test_multi_asset_bin_keeps_primary(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("bin")["id"]
        first = services.ingest(project_id, str(synthetic_video))
        second = services.add_media_asset(project_id, str(synthetic_video))
        assets = services.list_media_assets(project_id)
        assert len(assets) == 2
        assert assets[0]["src"] == first["src"]
        assert assets[0]["is_primary"] is True
        assert assets[1]["src"] == second["src"]
        assert assets[1]["is_primary"] is False
        assert services.get_media_facts(project_id).src == first["src"]

        services.set_primary_asset(project_id, second["id"])
        assert services.get_media_facts(project_id).src == second["src"]
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_multi_source_mixed_specs_and_silent_audio(
    settings: Settings,
    synthetic_video: Path,
    ffmpeg_bin: str,
    tmp_path: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("mixed")["id"]
        first = services.ingest(project_id, str(synthetic_video))
        muted = tmp_path / "muted.mp4"
        import subprocess

        subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x240:rate=24:duration=2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(muted),
            ],
            check=True,
            capture_output=True,
        )
        second = services.add_media_asset(project_id, str(muted))
        primary_facts = MediaFacts.model_validate(first["facts"])
        second_facts = MediaFacts.model_validate(second["facts"])
        ir = EditIR(
            project_id=project_id,
            media_facts=primary_facts,
            source_facts={second["src"]: second_facts},
            sequence=[
                TrimOp(
                    src=first["src"],
                    range=TimeRange(start=_rt("0"), end=_rt("1")),
                ),
                TrimOp(
                    src=second["src"],
                    range=TimeRange(start=_rt("0"), end=_rt("1")),
                ),
            ],
        )
        plan = lower(ir)
        cuts = [call for call in plan.tool_calls if call.tool == "cut"]
        assert cuts[1].args["ensure_audio"] is True
        assert cuts[1].args["fps"] == primary_facts.fps
        assert cuts[1].args["audio_channels"] == 2

        submitted = services.submit_revision(project_id, ir)
        result = services.render(project_id, int(submitted["revision"]))
        workspace = services.workspaces.get(project_id)
        actual = services._executor(workspace).probe(result["output_path"])
        assert actual.has_audio is True
        assert actual.video is not None
        assert actual.video.width == primary_facts.video.width
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_multi_source_ir_lowers_and_renders(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("multi")["id"]
        first = services.ingest(project_id, str(synthetic_video))
        second = services.add_media_asset(project_id, str(synthetic_video))
        primary_facts = MediaFacts.model_validate(first["facts"])
        second_facts = MediaFacts.model_validate(second["facts"])
        ir = EditIR(
            project_id=project_id,
            media_facts=primary_facts,
            source_facts={second["src"]: second_facts},
            sequence=[
                TrimOp(
                    src=first["src"],
                    range=TimeRange(start=_rt("0"), end=_rt("1")),
                ),
                TrimOp(
                    src=second["src"],
                    range=TimeRange(start=_rt("0.5"), end=_rt("1.5")),
                ),
            ],
        )
        plan = lower(ir)
        cuts = [call for call in plan.tool_calls if call.tool == "cut"]
        assert [call.args["src"] for call in cuts] == [
            first["src"],
            second["src"],
        ]
        assert cuts[0].args.get("fps") == primary_facts.fps

        submitted = services.submit_revision(project_id, ir)
        result = services.render(project_id, int(submitted["revision"]))
        output = Path(result["output_abs"])
        assert output.is_file()
        workspace = services.workspaces.get(project_id)
        actual = services._executor(workspace).probe(result["output_path"])
        assert abs(actual.duration.to_float() - 2.0) < 0.2
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()
