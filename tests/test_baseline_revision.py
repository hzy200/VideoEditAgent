from __future__ import annotations

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


def test_ensure_baseline_revision_is_idempotent(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("baseline")["id"]
        services.ingest(project_id, str(synthetic_video))
        services.ensure_baseline_revision(project_id)
        services.ensure_baseline_revision(project_id)
        revisions = services.list_revisions(project_id)
        assert len(revisions) == 1
        ir = revisions[0]["ir"]
        assert ir["media_facts"]["src"] == "media/source.mp4"
        sequence = ir["sequence"]
        assert len(sequence) == 1
        assert float(sequence[0]["range"]["start"]["value"]) == 0.0
        assert sequence[0]["range"]["end"]["value"] > 0
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()
