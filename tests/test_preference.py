from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.time import RationalTime
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database, MediaAsset
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _ir(project_id: str, facts: MediaFacts, spans: list[tuple[str, str]]) -> EditIR:
    return EditIR(
        project_id=project_id,
        revision=0,
        media_facts=facts,
        sequence=[
            TrimOp(
                src=facts.src,
                range=TimeRange(start=_rt(start), end=_rt(end)),
                tag=f"t{index}",
            )
            for index, (start, end) in enumerate(spans)
        ],
    )


def _services(settings: Settings) -> tuple[Services, JobService, Database]:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    return Services(settings, db, jobs, transcriber=NullTranscriber()), jobs, db


def test_user_edits_produce_preference_stats(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("pref")["id"]
        services.ingest(project_id, str(synthetic_video))
        facts = services.get_media_facts(project_id)

        services.submit_revision(
            project_id,
            _ir(project_id, facts, [("0", "1")]),
            origin="user",
        )
        services.submit_revision(
            project_id,
            _ir(project_id, facts, [("0", "0.5"), ("1", "1.5")]),
            origin="user",
        )

        preference = services.get_edit_preference(project_id)

        assert preference is not None
        stats = preference["stats"]
        assert stats["sample_size"] == 2
        assert stats["segment_count"] == 3
        assert stats["median_segment_seconds"] == 0.5
        assert stats["max_segment_seconds"] == 1.0
        assert stats["keep_ratio"] == 0.5
        assert preference["source_hash"]
        assert preference["notes"]
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_rejected_tags_are_excluded_from_preference(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("pref-reject")["id"]
        services.ingest(project_id, str(synthetic_video))
        facts = services.get_media_facts(project_id)
        services.submit_revision(
            project_id,
            _ir(project_id, facts, [("0", "0.8"), ("1", "1.8")]),
            origin="user",
        )
        services.submit_revision(
            project_id,
            _ir(project_id, facts, [("0", "0.8"), ("1", "1.8")]),
            origin="user",
        )
        result = services.record_segment_reject(
            project_id,
            [{"start": 1.0, "end": 1.8, "tag": "t1"}],
        )
        assert result["recorded"] == 1

        preference = services.get_edit_preference(project_id)
        assert preference is not None
        assert preference["stats"]["top_tags"] == ["t0"]
        assert any("被拒标签" in note for note in preference["notes"])
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_preference_is_scoped_to_current_source(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("pref-scope")["id"]
        services.ingest(project_id, str(synthetic_video))
        facts = services.get_media_facts(project_id)
        services.submit_revision(
            project_id,
            _ir(project_id, facts, [("0", "1")]),
            origin="user",
        )
        services.submit_revision(
            project_id,
            _ir(project_id, facts, [("0", "1")]),
            origin="user",
        )
        assert services.get_edit_preference(project_id) is not None

        with db.session() as session:
            asset = session.scalars(
                select(MediaAsset).where(MediaAsset.project_id == project_id)
            ).first()
            assert asset is not None
            asset.source_hash = "different_source_hash"

        assert services.get_edit_preference(project_id) is None
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_migration_backfills_feedback_source_hash_index(tmp_path: Path) -> None:
    db = Database(tmp_path / "migration.db")
    db.init()
    try:
        with db.engine.begin() as conn:
            conn.exec_driver_sql(
                "DROP INDEX IF EXISTS ix_edit_feedback_source_hash"
            )
        db._migrate()
        with db.engine.connect() as conn:
            names = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA index_list('edit_feedback')"
                )
            }
        assert "ix_edit_feedback_source_hash" in names
    finally:
        db.dispose()


def test_system_revisions_do_not_count_as_preference(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("pref-system")["id"]
        services.ingest(project_id, str(synthetic_video))
        facts = services.get_media_facts(project_id)

        services.submit_revision(project_id, _ir(project_id, facts, [("0", "1")]))
        services.submit_revision(project_id, _ir(project_id, facts, [("0", "1")]))

        assert services.get_edit_preference(project_id) is None
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()
