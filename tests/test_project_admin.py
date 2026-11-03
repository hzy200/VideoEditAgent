from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from smartcut.config import Settings
from smartcut.errors import IRValidationError, ResourceNotFound
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.main import create_app
from smartcut.server.services import Services


def _wait_job(services: Services, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = services.jobs.get(job_id)
        if job and job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job timeout: {job_id}")


def _make_app(settings: Settings):
    return create_app(settings, transcriber=NullTranscriber())


def test_rename_delete_project_cascade_and_active_job_guard(settings: Settings) -> None:
    app = _make_app(settings)
    with TestClient(app) as client:
        services: Services = app.state.services
        project = services.create_project("old name")
        project_id = project["id"]
        workspace = services.project_workspace(project_id)

        renamed = services.rename_project(project_id, "  new name  ")
        assert renamed["name"] == "new name"
        with pytest.raises(IRValidationError):
            services.rename_project(project_id, "   ")

        gate = threading.Event()

        def blocking(_reporter):
            gate.wait(5)
            return {"ok": True}

        job_id = services.jobs.submit(project_id, "block", blocking)
        with pytest.raises(IRValidationError):
            services.delete_project(project_id)
        gate.set()
        assert _wait_job(services, job_id)["status"] == "succeeded"

        jobs = services.jobs.list_jobs(project_id)
        assert [job["id"] for job in jobs] == [job_id]

        services._perception_cache[project_id] = ((), {"stale": True})
        assert services.delete_project(project_id) == {"deleted": project_id}
        assert project_id not in services._perception_cache
        assert not workspace.root.exists()
        with pytest.raises(ResourceNotFound):
            services.get_project(project_id)
        assert services.jobs.list_jobs(project_id) == []

        # HTTP 层：删除后再次访问返回 404。
        assert client.get(f"/api/projects/{project_id}").status_code == 404


def test_job_submit_rejected_after_project_deleted(settings: Settings) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project_id = services.create_project("deleted")["id"]
        services.delete_project(project_id)
        with pytest.raises(IRValidationError):
            jobs.submit(project_id, "late", lambda reporter: {})
    finally:
        services.close()
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        db.dispose()


def test_delete_project_blocks_concurrent_job_submit(
    settings: Settings,
    monkeypatch,
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 2)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    project_id = services.create_project("race")["id"]
    started = threading.Event()
    release = threading.Event()
    original = jobs.cancel_project

    def slow_cancel(project_id: str, *, timeout: float = 15.0) -> bool:
        started.set()
        release.wait(3)
        return original(project_id, timeout=timeout)

    monkeypatch.setattr(jobs, "cancel_project", slow_cancel)
    errors: list[BaseException] = []

    def submit_late() -> None:
        try:
            jobs.submit(project_id, "late", lambda reporter: {})
        except BaseException as exc:
            errors.append(exc)

    deleter = threading.Thread(target=services.delete_project, args=(project_id,))
    submitter = threading.Thread(target=submit_late)
    try:
        deleter.start()
        assert started.wait(2)
        submitter.start()
        submitter.join(timeout=5)
        release.set()
        deleter.join(timeout=5)
        assert not deleter.is_alive()
        assert errors and isinstance(errors[0], IRValidationError)
    finally:
        release.set()
        if deleter.is_alive():
            deleter.join(timeout=5)
        services.close()
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        db.dispose()


def test_diagnostics_and_logs_read_tail(settings: Settings) -> None:
    settings.ensure_dirs()
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    settings.log_file.write_text(
        "".join(f"line-{index}\n" for index in range(10)),
        encoding="utf-8",
    )
    app = _make_app(settings)
    with TestClient(app) as client:
        services: Services = app.state.services
        logs = services.read_logs(limit=3)
        assert logs == ["line-7\n", "line-8\n", "line-9\n"]
        assert services.read_logs(limit=0) == ["line-9\n"]

        diagnostics = services.diagnostics(limit=2)
        assert diagnostics["logs"] == ["line-8\n", "line-9\n"]
        assert Path(diagnostics["info"]["workspace_root"]) == settings.workspace_root
        assert diagnostics["info"]["active_jobs"] >= 0

        response = client.get("/api/diagnostics", params={"limit": 1})
        assert response.status_code == 200
        assert response.json()["logs"] == ["line-9\n"]


def test_jobs_api_and_sse_events(settings: Settings) -> None:
    app = _make_app(settings)
    with TestClient(app) as client:
        services: Services = app.state.services
        project = services.create_project("jobs")
        project_id = project["id"]
        job_id = services.jobs.submit(project_id, "quick", lambda _reporter: {"ok": 1})
        assert _wait_job(services, job_id)["status"] == "succeeded"

        listed = client.get("/api/jobs", params={"project_id": project_id})
        assert listed.status_code == 200
        assert [job["id"] for job in listed.json()["jobs"]] == [job_id]

        events = client.get(f"/api/jobs/{job_id}/events")
        assert events.status_code == 200
        assert "text/event-stream" in events.headers["content-type"]
        assert f'"id": "{job_id}"' in events.text
        assert '"succeeded"' in events.text

        missing = client.get("/api/jobs/not-a-job/events")
        assert missing.status_code == 200
        assert "job not found" in missing.text


def test_patch_and_delete_project_api(settings: Settings) -> None:
    app = _make_app(settings)
    with TestClient(app) as client:
        created = client.post("/api/projects", json={"name": "api"}).json()
        project_id = created["id"]

        patched = client.patch(
            f"/api/projects/{project_id}", json={"name": "renamed"}
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["name"] == "renamed"

        invalid = client.patch(f"/api/projects/{project_id}", json={"name": "  "})
        assert invalid.status_code in {400, 422}

        deleted = client.delete(f"/api/projects/{project_id}")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json() == {"deleted": project_id}
        assert client.get(f"/api/projects/{project_id}").status_code == 404
