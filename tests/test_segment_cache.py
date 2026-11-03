from __future__ import annotations

from pathlib import Path

import pytest

from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.time import RationalTime
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _ir(project_id: str, facts, ranges: list[tuple[str, str]]) -> EditIR:
    return EditIR(
        project_id=project_id,
        revision=0,
        media_facts=facts,
        sequence=[
            TrimOp(
                src=facts.src,
                range=TimeRange(start=_rt(start), end=_rt(end)),
                tag="keep",
                confidence=0.9,
            )
            for start, end in ranges
        ],
    )


def test_revision_allocation_retries_unique_conflict(
    settings: Settings,
    synthetic_video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project_id = services.create_project("revision-race")["id"]
        services.ingest(project_id, str(synthetic_video))
        facts = services.get_media_facts(project_id)
        first = services.submit_revision(
            project_id,
            _ir(project_id, facts, [("0.2", "0.8")]),
        )
        assert first["revision"] == 1

        # 模拟两个并发请求都读到 max+1=1：前两次冲突，第三次重新分配为 2。
        calls = iter([1, 1, 2])
        monkeypatch.setattr(
            services,
            "_next_revision_no",
            lambda session, project: next(calls),
        )
        second = services.submit_revision(
            project_id,
            _ir(project_id, facts, [("0.2", "0.8")]),
        )
        assert second["revision"] == 2
    finally:
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        services.close()


def test_segment_cache_and_ir_diff(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project = services.create_project("segments")
        project_id = project["id"]
        services.ingest(project_id, str(synthetic_video))
        facts = services.get_media_facts(project_id)
        workspace = services.project_workspace(project_id)

        first = services.submit_revision(
            project_id,
            _ir(project_id, facts, [("0.2", "0.8"), ("1.0", "1.6")]),
        )
        result1 = services.render(project_id, first["revision"])
        assert result1["cache_hits"] == 0
        assert result1["cache_misses"] == 2
        assert all(not item["cached"] for item in result1["results"] if item["tool"] == "cut")
        assert (workspace.root / result1["output_path"]).exists()

        # 同一 revision 再渲染：两个 cut 都走片段缓存。
        result2 = services.render(project_id, first["revision"])
        assert result2["cache_hits"] == 2
        assert result2["cache_misses"] == 0
        assert all(item["cached"] for item in result2["results"] if item["tool"] == "cut")

        # 只改第二段：第一段命中缓存，第二段重新渲染。
        second = services.submit_revision(
            project_id,
            _ir(project_id, facts, [("0.2", "0.8"), ("1.0", "1.4")]),
        )
        result3 = services.render(project_id, second["revision"])
        assert result3["cache_hits"] == 1
        assert result3["cache_misses"] == 1

        diff = services.revision_diff(
            project_id,
            second["revision"],
            first["revision"],
        )
        assert diff["unchanged_count"] == 1
        assert diff["added_count"] == 1
        assert diff["removed_count"] == 1
        assert diff["added"][0]["start_seconds"] == "1"
        assert diff["added"][0]["end_seconds"] == "1.4"
        assert diff["removed"][0]["end_seconds"] == "1.6"
        assert diff["effects_changed"] is False
        assert diff["audio_changed"] is False
        assert diff["export_changed"] is False
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
