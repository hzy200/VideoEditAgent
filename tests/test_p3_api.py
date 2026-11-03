from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from smartcut.config import Settings
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.main import create_app


def _wait_for_job(client: TestClient, job_id: str, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"任务超时: {job_id}")


def test_template_crud_and_batch_fast_outline(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    app = create_app(settings, transcriber=NullTranscriber())
    with TestClient(app) as client:
        created = client.post(
            "/api/templates",
            json={
                "name": "访谈快速大纲",
                "goal": "提炼访谈主题",
                "phase": "fast_outline",
                "allow_render": False,
                "burn_captions": True,
            },
        )
        assert created.status_code == 200, created.text
        template = created.json()
        assert template["id"]
        assert template["burn_captions"] is True

        listed = client.get("/api/templates").json()["templates"]
        assert [item["id"] for item in listed] == [template["id"]]

        project_ids: list[str] = []
        for index in range(2):
            project = client.post(
                "/api/projects",
                json={"name": f"batch-{index}"},
            ).json()
            project_ids.append(project["id"])
            ingested = client.post(
                f"/api/projects/{project['id']}/ingest",
                json={"src": str(synthetic_video)},
            )
            assert ingested.status_code == 200, ingested.text

        response = client.post(
            "/api/batch/orchestrate",
            json={
                "project_ids": project_ids,
                "template_id": template["id"],
            },
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert len(body["runs"]) == 2
        assert "烧录字幕" in body["goal"]
        job = _wait_for_job(client, body["job_id"])
        assert job["status"] == "succeeded", job
        assert job["result"]["failed"] == 0
        assert job["result"]["succeeded"] == 2

        for item in body["runs"]:
            run = client.get(f"/api/runs/{item['run_id']}").json()
            assert run["status"] == "awaiting_approval"
            assert run["phase"] == "fast_outline"
            assert "提炼访谈主题" in run["goal"]
            outline = client.get(f"/api/runs/{item['run_id']}/outline").json()
            assert outline["chapters"]

        deleted = client.delete(f"/api/templates/{template['id']}")
        assert deleted.status_code == 200
        assert client.get("/api/templates").json()["templates"] == []
