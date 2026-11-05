from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.main import create_app


def _wait(client: TestClient, job_id: str) -> dict:
    deadline = time.time() + 90
    job: dict = {}
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.05)
    return job


def test_media_api_diagnose_repair_and_interpret(
    settings,
    synthetic_video: Path,
    tmp_path: Path,
) -> None:
    app = create_app(settings, transcriber=NullTranscriber())
    with TestClient(app) as client:
        project_id = client.post(
            "/api/projects", json={"name": "media-api"}
        ).json()["id"]
        assert (
            client.post(
                f"/api/projects/{project_id}/ingest",
                json={"src": str(synthetic_video)},
            ).status_code
            == 200
        )

        diagnosis = client.post(
            f"/api/projects/{project_id}/media/diagnose", json={}
        )
        assert diagnosis.status_code == 200, diagnosis.text
        assert diagnosis.json()["probe_ok"] is True

        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"not a media file")
        imported = client.post(
            f"/api/projects/{project_id}/media/import",
            json={"src": str(broken)},
        )
        assert imported.status_code == 200, imported.text
        raw_src = imported.json()["src"]
        assert raw_src.startswith("media/")
        broken_diagnosis = client.post(
            f"/api/projects/{project_id}/media/diagnose",
            json={"src": raw_src},
        )
        assert broken_diagnosis.status_code == 200
        assert broken_diagnosis.json()["probe_ok"] is False

        plan = client.post(
            f"/api/projects/{project_id}/media/interpret",
            json={"goal": "转成 mkv", "use_llm": False},
        )
        assert plan.status_code == 200, plan.text
        payload = plan.json()
        assert payload["operation"] == "convert"
        assert payload["transcode"]["profile"]["container"] == "mkv"

        unknown = client.post(
            f"/api/projects/{project_id}/media/interpret",
            json={"goal": "剪成三条短视频", "use_llm": False},
        )
        assert unknown.status_code == 422

        repair = client.post(
            f"/api/projects/{project_id}/media/repair",
            json={"strategy": "auto"},
        )
        assert repair.status_code == 202, repair.text
        job = _wait(client, repair.json()["job_id"])
        assert job["status"] == "succeeded", job
        assert job["result"]["output_path"].startswith("repairs/")

        operation = client.post(
            f"/api/projects/{project_id}/media/operation",
            json={"goal": "转成 mkv", "use_llm": False},
        )
        assert operation.status_code == 202, operation.text
        op_job = _wait(client, operation.json()["job_id"])
        assert op_job["status"] == "succeeded", op_job
        assert op_job["result"]["operation"] == "convert"
        assert op_job["result"]["output_path"].endswith(".mkv")
