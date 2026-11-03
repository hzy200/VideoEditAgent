from __future__ import annotations

from fastapi.testclient import TestClient

from smartcut.config import Settings
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.main import create_app


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings, transcriber=NullTranscriber()))


def test_unknown_project_endpoints_return_404(settings: Settings) -> None:
    with _client(settings) as client:
        assert client.get("/api/projects/nope").status_code == 404
        assert client.patch(
            "/api/projects/nope", json={"name": "x"}
        ).status_code == 404
        assert client.delete("/api/projects/nope").status_code == 404
        assert client.get("/api/projects/nope/audio-assets").status_code == 404
        assert client.get("/api/projects/nope/revisions").status_code == 404


def test_template_id_rejects_path_traversal(settings: Settings) -> None:
    with _client(settings) as client:
        response = client.delete("/api/templates/..%5C..%5Cevil")
        assert response.status_code == 422, response.text


def test_invalid_revision_ir_returns_422(settings: Settings) -> None:
    with _client(settings) as client:
        project_id = client.post("/api/projects", json={"name": "bad"}).json()["id"]
        response = client.post(
            f"/api/projects/{project_id}/revisions",
            json={
                "ir": {
                    "schema": "editir/v1",
                    "project_id": project_id,
                    "media_facts": {
                        "src": "media/none.mp4",
                        "duration": {"value": 90_000, "rate": 90_000},
                        "video": {
                            "codec_type": "video",
                            "width": 640,
                            "height": 360,
                        },
                    },
                    "sequence": [],
                }
            },
        )
        assert response.status_code == 422


def test_revision_requires_ingested_asset(settings: Settings) -> None:
    with _client(settings) as client:
        project_id = client.post("/api/projects", json={"name": "empty"}).json()["id"]
        response = client.post(
            f"/api/projects/{project_id}/revisions",
            json={"ir": {"project_id": project_id, "sequence": []}},
        )
        assert response.status_code in {404, 422}


def test_diagnostics_and_jobs_filters(settings: Settings) -> None:
    with _client(settings) as client:
        diagnostics = client.get("/api/diagnostics", params={"limit": 1})
        assert diagnostics.status_code == 200
        info = diagnostics.json()["info"]
        assert "demucs_available" in info
        assert "active_jobs" in info
        jobs = client.get("/api/jobs", params={"project_id": "missing"})
        assert jobs.status_code == 200
        assert jobs.json()["jobs"] == []


def test_health_and_outline_missing(settings: Settings) -> None:
    with _client(settings) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        assert client.get("/api/runs/missing").status_code == 404
        assert client.get("/api/runs/missing/outline").status_code == 404
