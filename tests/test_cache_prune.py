from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from smartcut.server.cache import ArtifactCache
from smartcut.server.db import Artifact, Database, Project
from smartcut.server.jobs import JobService


def _put(
    cache: ArtifactCache,
    db: Database,
    project_id: str,
    kind: str,
    key: str,
    size: int,
    *,
    updated_at: datetime,
) -> Path:
    path = cache.cache_path(kind, key, ".bin")
    path.write_bytes(b"x" * size)
    cache.put(project_id, kind, key, path)
    with db.session() as session:
        row = session.scalar(
            select(Artifact).where(
                Artifact.project_id == project_id,
                Artifact.kind == kind,
                Artifact.cache_key == key,
            )
        )
        assert row is not None
        row.updated_at = updated_at
    return path


def test_prune_only_removes_recomputable_kinds(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    db.init()
    workspace = tmp_path / "workspace"
    cache = ArtifactCache(db, workspace)
    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2026, 1, 2, tzinfo=timezone.utc)
    try:
        frames = _put(cache, db, "p", "frames", "k1", 100, updated_at=older)
        scenes = _put(cache, db, "p", "scenes", "k2", 100, updated_at=newer)
        segments = _put(cache, db, "p", "segments", "k3", 1000, updated_at=older)
        renders = _put(cache, db, "p", "renders", "k4", 1000, updated_at=older)
        manifest = _put(cache, db, "p", "manifest", "k5", 1000, updated_at=older)
        result = _put(cache, db, "p", "result", "k6", 1000, updated_at=older)

        report = cache.prune("p", max_bytes=100)

        assert report["pruned"] == 1
        assert report["freed_bytes"] == 100
        assert report["remaining_bytes"] == 100
        assert not frames.exists()
        for kept in (scenes, segments, renders, manifest, result):
            assert kept.exists(), kept
        remaining_kinds = {
            row.kind for row in cache.list_records("p", "frames")
        }
        assert remaining_kinds == set()
    finally:
        db.dispose()


def test_prune_dry_run_keeps_files(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    db.init()
    cache = ArtifactCache(db, tmp_path / "workspace")
    try:
        frame = _put(
            cache,
            db,
            "p",
            "frames",
            "k1",
            100,
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        report = cache.prune("p", max_bytes=1, dry_run=True)
        assert report["pruned"] == 1
        assert frame.exists()
        assert cache.get("p", "frames", "k1") is not None
    finally:
        db.dispose()


def test_prune_skips_project_with_active_jobs(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    db.init()
    with db.session() as session:
        session.add(Project(id="p", name="p", workspace_dir=str(tmp_path)))
    jobs = JobService(db, 1)
    cache = ArtifactCache(db, tmp_path / "workspace")
    frame = _put(
        cache,
        db,
        "p",
        "frames",
        "k1",
        100,
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    gate = threading.Event()
    started = threading.Event()

    def blocker(_reporter):
        started.set()
        gate.wait(5)
        return {"ok": True}

    job_id = jobs.submit("p", "block", blocker)
    try:
        assert started.wait(2)
        report = jobs.prune_project_cache("p", cache, max_bytes=1)
        assert report["skipped"] is True
        assert report["reason"] == "active_jobs"
        assert frame.exists()
    finally:
        gate.set()
        jobs.cancel(job_id)
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        db.dispose()
