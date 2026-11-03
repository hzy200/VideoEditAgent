from __future__ import annotations

import threading
import time

import pytest

from smartcut.config import Settings
from smartcut.errors import IRValidationError
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database, Job, OrchestrationRun
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _services(settings: Settings) -> tuple[Services, JobService, Database]:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    return Services(settings, db, jobs, transcriber=NullTranscriber()), jobs, db


def _prepare(settings: Settings, source) -> tuple[Services, JobService, Database, str, str]:
    services, jobs, db = _services(settings)
    project_id = services.create_project("recovery")["id"]
    services.ingest(project_id, str(source))
    run = services.create_orchestration_run(project_id, "goal", allow_render=False)
    return services, jobs, db, project_id, run["id"]


def test_orchestration_run_rejects_duplicate_execution(
    settings: Settings, synthetic_video
) -> None:
    services, jobs, db, _project_id, run_id = _prepare(settings, synthetic_video)
    try:
        with db.session() as session:
            run = session.get(OrchestrationRun, run_id)
            run.status = "running"

        with pytest.raises(IRValidationError):
            services.orchestrate_run(run_id)
    finally:
        services.close()
        jobs.shutdown()


def test_interrupted_run_is_listed_and_can_be_resumed(
    settings: Settings, synthetic_video
) -> None:
    services, jobs, db, project_id, run_id = _prepare(settings, synthetic_video)
    try:
        with db.session() as session:
            run = session.get(OrchestrationRun, run_id)
            run.status = "interrupted"

        resumable = services.list_resumable_runs(project_id)
        assert [item["id"] for item in resumable] == [run_id]

        result = services.resume_orchestration_run(run_id)
        assert result["resumed_from"] == run_id
        assert result["run_id"] != run_id
        jobs.cancel(result["job_id"])

        with db.session() as session:
            old = session.get(OrchestrationRun, run_id)
            assert old.status == "resumed"
        assert services.list_resumable_runs(project_id) == []
    finally:
        services.close()
        jobs.shutdown()


def test_concurrent_same_run_only_one_executes(
    settings: Settings, synthetic_video
) -> None:
    services, jobs, db, _project_id, run_id = _prepare(settings, synthetic_video)
    barrier = threading.Barrier(2)
    results: list[tuple[str, str]] = []

    def worker() -> None:
        barrier.wait()
        try:
            result = services.orchestrate_run(run_id)
            results.append(("ok", result["status"]))
        except Exception as exc:  # 只允许一个 CAS 成功
            results.append(("error", type(exc).__name__))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert all(not thread.is_alive() for thread in threads)
        assert sum(1 for kind, _ in results if kind == "ok") == 1
        assert sum(
            1
            for kind, value in results
            if kind == "error" and value == "IRValidationError"
        ) == 1
    finally:
        services.close()
        jobs.shutdown()


def test_cancelling_queued_orchestration_job_marks_run_cancelled(
    settings: Settings, synthetic_video
) -> None:
    services, jobs, db, project_id, run_id = _prepare(settings, synthetic_video)
    gate = threading.Event()
    blocker = jobs.submit(project_id, "block", lambda reporter: gate.wait(5))
    job_id = jobs.submit(
        project_id,
        "orchestrate",
        lambda reporter: services.orchestrate_run(run_id, reporter),
        on_cancel=lambda: services.cancel_orchestration_run(run_id),
    )
    try:
        assert jobs.cancel(job_id) is True
        with db.session() as session:
            run = session.get(OrchestrationRun, run_id)
            assert run.status == "cancelled"
    finally:
        gate.set()
        jobs.cancel(blocker)
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        services.close()


def test_orchestration_parse_failure_marks_run_failed(
    settings: Settings, synthetic_video
) -> None:
    services, jobs, db, _project_id, run_id = _prepare(settings, synthetic_video)
    try:
        with db.session() as session:
            run = session.get(OrchestrationRun, run_id)
            run.manual_ir_json = "{not-json"
        with pytest.raises(Exception):
            services.orchestrate_run(run_id)
        with db.session() as session:
            run = session.get(OrchestrationRun, run_id)
            assert run.status == "failed"
            assert run.error
    finally:
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        services.close()


def test_resume_rejects_running_or_succeeded_run(
    settings: Settings, synthetic_video
) -> None:
    services, jobs, db, _project_id, run_id = _prepare(settings, synthetic_video)
    try:
        with db.session() as session:
            run = session.get(OrchestrationRun, run_id)
            run.status = "running"
        with pytest.raises(IRValidationError):
            services.resume_orchestration_run(run_id)
        with db.session() as session:
            run = session.get(OrchestrationRun, run_id)
            assert run.status == "running"
    finally:
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        services.close()


def test_startup_marks_interrupted_jobs_and_runs(
    settings: Settings, synthetic_video
) -> None:
    services, jobs, db, project_id, run_id = _prepare(settings, synthetic_video)
    try:
        with db.session() as session:
            run = session.get(OrchestrationRun, run_id)
            run.status = "running"
            session.add(
                Job(
                    id="job_running",
                    project_id=project_id,
                    kind="orchestrate",
                    status="running",
                    progress=10,
                    message="执行中",
                )
            )

        restarted = Services(settings, db, jobs, transcriber=NullTranscriber())
        try:
            with db.session() as session:
                run = session.get(OrchestrationRun, run_id)
                job = session.get(Job, "job_running")
                assert run.status == "interrupted"
                assert job.status == "interrupted"
        finally:
            restarted.close()
    finally:
        services.close()
        jobs.shutdown()
