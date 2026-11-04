from __future__ import annotations

import json
from pathlib import Path

from smartcut.config import Settings
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _services(settings: Settings) -> tuple[Services, JobService, Database]:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    return Services(settings, db, jobs, transcriber=NullTranscriber()), jobs, db


def test_build_visual_evidence_and_cache(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("evidence")["id"]
        services.ingest(project_id, str(synthetic_video))
        services.extract_frames(project_id)
        first = services.build_visual_evidence(project_id)
        assert first["available"] is True, first
        assert first["cached"] is False
        evidence = first["evidence"]
        assert evidence["time_role"] == "candidate_only"
        assert evidence["tile_map"]["tiles"]
        assert evidence["tile_map"]["tiles"][0]["label"] == "T01"
        assert evidence["render_hash"].startswith("sha256:")
        assert evidence["semantic_hash"].startswith("dhash:")
        assert evidence["meta"]["backend"] == "cpu"
        assert first["metrics"]["tiles"] == len(evidence["tile_map"]["tiles"])

        directory = services.project_workspace(project_id).root / first["path"]
        assert (directory / "view.png").is_file()
        payload = json.loads((directory / "evidence.json").read_text("utf-8"))
        assert payload["id"] == evidence["id"]
        assert services._take_evidence_metrics()["tiles"] >= 1

        second = services.build_visual_evidence(project_id)
        assert second["available"] is True
        assert second["cached"] is True
        assert second["metrics"]["cache_hits"] == 1
        assert second["evidence"]["render_hash"] == evidence["render_hash"]
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_evidence_requires_frames(settings: Settings, synthetic_video: Path) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("evidence-no-frames")["id"]
        services.ingest(project_id, str(synthetic_video))
        result = services.build_visual_evidence(project_id)
        assert result["available"] is False
        assert "尚未抽帧" in result["warning"]
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


class BrokenRenderer:
    backend = "qtgl"

    def render(self, measure, request):
        raise RuntimeError("gl boom")


def test_evidence_falls_back_to_cpu_when_gpu_fails(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(
        settings,
        db,
        jobs,
        transcriber=NullTranscriber(),
        view_renderer=BrokenRenderer(),
    )
    try:
        project_id = services.create_project("evidence-fallback")["id"]
        services.ingest(project_id, str(synthetic_video))
        services.extract_frames(project_id)
        result = services.build_visual_evidence(project_id)
        assert result["available"] is True, result
        assert result["metrics"]["backend"] == "cpu"
        assert result["metrics"]["renderer_fallback"] is True
        assert result["evidence"]["meta"]["backend"] == "cpu"
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_evidence_can_be_disabled(settings: Settings) -> None:
    services, jobs, db = _services(settings)
    try:
        services.settings.view_evidence_enabled = False
        result = services.build_visual_evidence("p_missing")
        assert result["available"] is False
        assert "已关闭" in result["warning"]
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
