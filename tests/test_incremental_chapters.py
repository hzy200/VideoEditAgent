from __future__ import annotations

from pathlib import Path

from smartcut.config import Settings
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def test_chapter_scoped_quality_does_not_invalidate_full_cache(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    settings.chapter_target_seconds = 1.0
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project = services.create_project("incremental")
        project_id = project["id"]
        services.ingest(project_id, str(synthetic_video))
        frames = services.extract_frames(project_id)
        assert frames["count"] > 0
        services.detect_scenes(project_id)
        services.build_contact_sheet(project_id)
        services.build_chapters(project_id)
        chapters = services.get_chapters(project_id)
        assert chapters is not None
        first_id = chapters["chapters"][0]["id"]

        cache = services._cache_for(project_id)
        full = services.analyze_quality(project_id)
        assert full["available"] is True
        full_record = cache.latest(project_id, "quality")
        assert full_record is not None

        scoped = services.analyze_quality(project_id, chapter_ids=[first_id])
        assert scoped["available"] is True
        assert scoped["analyzed_frames"] <= full["analyzed_frames"]
        assert scoped["duration_ms"] >= 0
        assert cache.latest(project_id, "quality")["cache_key"] == full_record["cache_key"]
        chapter_records = cache.list_records(project_id, "quality_chapter")
        assert len(chapter_records) == 1
        assert chapter_records[0]["meta"]["chapter_ids"] == [first_id]

        cached = services.analyze_quality(project_id, chapter_ids=[first_id])
        assert cached["cached"] is True
        assert cached["analyzed_frames"] == scoped["analyzed_frames"]

        frames_scoped = services.extract_frames(project_id, chapter_ids=[first_id])
        assert frames_scoped["scoped_count"] <= frames_scoped["count"]
        assert frames_scoped["chapter_ids"] == [first_id]

        manifest = services.build_video_manifest(
            project_id,
            chapter_ids=[first_id],
        )["manifest"]
        assert [item["id"] for item in manifest["chapters"]["chapters"]] == [
            first_id
        ]
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
