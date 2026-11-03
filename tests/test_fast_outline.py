from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.time import RationalTime
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.main import create_app


class CapturingPlanner:
    available = True

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def propose(self, facts, transcript, goal, **kwargs) -> EditIR:
        self.calls.append(kwargs)
        return EditIR(
            project_id=kwargs["project_id"],
            revision=kwargs["revision"],
            media_facts=facts,
            sequence=[
                TrimOp(
                    src=facts.src,
                    range=TimeRange(
                        start=RationalTime.from_seconds("0.2", rate=90_000),
                        end=RationalTime.from_seconds("1.2", rate=90_000),
                    ),
                    confidence=0.9,
                )
            ],
        )

    def close(self) -> None:
        pass


def _wait_for_job(client: TestClient, job_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"任务超时: {job_id}")


def test_fast_outline_skips_expensive_stages_then_resumes_phase_b(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    settings.chapter_target_seconds = 1.0
    planner = CapturingPlanner()
    app = create_app(
        settings,
        transcriber=NullTranscriber(),
        planner=planner,
    )
    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "fast-outline"}).json()
        project_id = project["id"]
        ingested = client.post(
            f"/api/projects/{project_id}/ingest",
            json={"src": str(synthetic_video)},
        )
        assert ingested.status_code == 200, ingested.text

        response = client.post(
            f"/api/projects/{project_id}/orchestrate",
            json={
                "goal": "先看章节结构",
                "allow_render": False,
                "phase": "fast_outline",
            },
        )
        assert response.status_code == 202, response.text
        job = _wait_for_job(client, response.json()["job_id"])
        assert job["status"] == "succeeded", job

        run = client.get(f"/api/runs/{response.json()['run_id']}").json()
        assert run["status"] == "awaiting_approval"
        assert run["phase"] == "fast_outline"
        agents = {item["agent"] for item in run["ledger"]}
        assert agents == {"analyze", "frames", "scenes", "contactsheet", "chapters"}
        assert not planner.calls
        assert not client.get(
            f"/api/projects/{project_id}/revisions"
        ).json()["revisions"]

        outline = client.get(f"/api/runs/{run['id']}/outline").json()
        chapters = outline["chapters"]
        assert chapters
        selected = chapters[0]

        approved = client.post(
            f"/api/runs/{run['id']}/approve-outline",
            json={
                "chapters": [
                    {
                        "id": selected["id"],
                        "title": selected.get("title"),
                        "start": selected["start"],
                        "end": selected["end"],
                    }
                ]
            },
        )
        assert approved.status_code == 202, approved.text
        body = approved.json()
        job_b = _wait_for_job(client, body["job_id"])
        assert job_b["status"] == "succeeded", job_b

        run_b = client.get(f"/api/runs/{body['run_id']}").json()
        assert run_b["status"] == "succeeded"
        assert run_b["phase"] == "plan"
        assert len(planner.calls) == 1
        assert planner.calls[0]["chapter_ids"] == [selected["id"]]
        assert [
            item["id"] for item in planner.calls[0]["chapters"]["chapters"]
        ] == [selected["id"]]
