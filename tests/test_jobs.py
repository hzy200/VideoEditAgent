from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from smartcut.contracts.tools import ToolResult
from smartcut.errors import IRValidationError, RunCancelledError
from smartcut.server.db import (
    Database,
    Job,
    Project,
    ToolCallRecord,
)
from smartcut.server.jobs import JobReporter, JobService


class FakeJobs:
    def __init__(self) -> None:
        self.batches: list[int] = []
        self.progress: list[tuple[int, str | None]] = []

    def update_progress(self, job_id: str, percent: int, message: str | None = None) -> None:
        self.progress.append((percent, message))

    def record_tools(self, job_id: str, items: list[tuple[int, ToolResult]]) -> None:
        self.batches.append(len(items))


def test_job_reporter_batches_tool_audit() -> None:
    jobs = FakeJobs()
    reporter = JobReporter(jobs, "job_test")  # type: ignore[arg-type]
    for index in range(45):
        reporter.record_tool(
            index,
            ToolResult(
                tool="cut",
                argv=[],
                exit_code=0,
                duration_ms=1,
                artifacts=[f"seg_{index}.mp4"],
            ),
        )
    assert jobs.batches == [20, 20]
    reporter.flush_tools()
    assert jobs.batches == [20, 20, 5]


def _job_service(tmp_path: Path) -> tuple[JobService, Database]:
    db = Database(tmp_path / "jobs.sqlite")
    db.init()
    with db.session() as session:
        session.add(Project(id="p_jobs", name="jobs", workspace_dir=str(tmp_path)))
    return JobService(db, 1), db


def test_queue_limit_rejects_when_full(tmp_path: Path) -> None:
    db = Database(tmp_path / "queue.sqlite")
    db.init()
    with db.session() as session:
        session.add(Project(id="p_jobs", name="jobs", workspace_dir=str(tmp_path)))
    jobs = JobService(db, 1, max_pending=1)
    gate = threading.Event()
    started = threading.Event()

    def blocker(reporter):
        started.set()
        gate.wait(5)
        return {"ok": True}

    job_id = jobs.submit("p_jobs", "block", blocker)
    try:
        assert started.wait(2)
        with pytest.raises(IRValidationError):
            jobs.submit("p_jobs", "overflow", lambda reporter: {})
    finally:
        gate.set()
        jobs.cancel(job_id)
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        db.dispose()


def test_shutdown_cancels_running_jobs_and_rejects_new_submit(tmp_path: Path) -> None:
    jobs, db = _job_service(tmp_path)
    started = threading.Event()

    def handler(reporter):
        started.set()
        while not reporter.cancel_token.cancelled:
            time.sleep(0.01)
        raise RunCancelledError("cancelled")

    try:
        job_id = jobs.submit("p_jobs", "block", handler)
        assert started.wait(2)
        assert jobs.shutdown(wait=True, timeout=5, cancel=True) is True
        job = jobs.get(job_id)
        assert job is not None and job["status"] == "cancelled"
        with pytest.raises(RuntimeError):
            jobs.submit("p_jobs", "late", lambda reporter: {})
    finally:
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        db.dispose()


def test_cancel_db_write_failure_still_calls_hook_and_cleans(
    tmp_path: Path,
) -> None:
    jobs, db = _job_service(tmp_path)
    gate = threading.Event()
    started = threading.Event()
    hook_calls: list[int] = []

    def blocker(reporter):
        started.set()
        gate.wait(5)
        return {"ok": True}

    blocker_id = jobs.submit("p_jobs", "block", blocker)
    assert started.wait(2)
    job_id = jobs.submit(
        "p_jobs",
        "queued",
        lambda reporter: {"ok": True},
        on_cancel=lambda: hook_calls.append(1),
    )
    original = jobs._update

    def failing(job_id: str, **fields):
        if fields.get("status") == "cancelled":
            raise RuntimeError("database is locked")
        return original(job_id, **fields)

    jobs._update = failing  # type: ignore[method-assign]
    try:
        assert jobs.cancel(job_id) is True
        assert hook_calls == [1]
        assert job_id not in jobs._tokens
        assert job_id not in jobs._futures
    finally:
        jobs._update = original  # type: ignore[method-assign]
        gate.set()
        jobs.cancel(blocker_id)
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        db.dispose()


def test_job_finish_purges_expired_tool_records(tmp_path: Path) -> None:
    jobs, db = _job_service(tmp_path)
    old_time = datetime.now(timezone.utc) - timedelta(days=45)
    with db.session() as session:
        session.add(Job(id="job_old", project_id="p_jobs", kind="old", status="succeeded"))
        session.add(
            ToolCallRecord(
                job_id="job_old",
                seq=0,
                tool="cut",
                argv_json="[]",
                created_at=old_time,
            )
        )
        session.add(
            ToolCallRecord(job_id="job_old", seq=1, tool="cut", argv_json="[]")
        )
    try:
        jobs.submit("p_jobs", "quick", lambda reporter: {"ok": True})
        assert jobs.shutdown(wait=True, timeout=5) is True
        with db.session() as session:
            records = session.scalars(
                select(ToolCallRecord).order_by(ToolCallRecord.seq)
            ).all()
        assert [record.seq for record in records] == [1]
    finally:
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        db.dispose()


def test_initial_status_write_failure_does_not_leak_job(tmp_path: Path) -> None:
    jobs, db = _job_service(tmp_path)
    original = jobs._update

    def flaky(job_id: str, **fields):
        if fields.get("status") == "running":
            raise RuntimeError("database is locked")
        return original(job_id, **fields)

    jobs._update = flaky  # type: ignore[method-assign]
    try:
        job_id = jobs.submit("p_jobs", "quick", lambda reporter: {"ok": True})
        deadline = time.monotonic() + 3
        job = None
        while time.monotonic() < deadline:
            job = jobs.get(job_id)
            if job is not None and job["status"] in {"failed", "cancelled", "succeeded"}:
                break
            time.sleep(0.02)
        assert job is not None and job["status"] == "failed"
        assert job_id not in jobs._tokens
        assert job_id not in jobs._futures
    finally:
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        db.dispose()
