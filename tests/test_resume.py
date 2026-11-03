from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from smartcut.config import Settings
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import OrchestrationRun
from smartcut.server.main import create_app


def test_resume_creates_new_run_and_job(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    app = create_app(settings, transcriber=NullTranscriber())
    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "resume"}).json()
        project_id = project["id"]
        assert (
            client.post(
                f"/api/projects/{project_id}/ingest",
                json={"src": str(synthetic_video)},
            ).status_code
            == 200
        )

        services = app.state.services
        old = services.create_orchestration_run(
            project_id,
            "分析一下",
            allow_render=False,
        )
        old_id = old["id"]
        with services.db.session() as session:
            stored = session.get(OrchestrationRun, old_id)
            stored.status = "interrupted"

        response = client.post(f"/api/runs/{old_id}/resume")
        assert response.status_code == 202, response.text
        payload = response.json()
        assert payload["run_id"] != old_id
        assert payload["resumed_from"] == old_id
        assert payload["project_id"] == project_id

        job_id = payload["job_id"]
        client.post(f"/api/jobs/{job_id}/cancel")

        deadline = time.time() + 30
        while time.time() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.05)

        runs = client.get(f"/api/projects/{project_id}/runs").json()["runs"]
        run_ids = {run["id"] for run in runs}
        assert old_id in run_ids
        assert payload["run_id"] in run_ids
