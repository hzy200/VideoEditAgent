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

    def propose(
        self,
        facts,
        transcript,
        goal,
        *,
        project_id: str,
        revision: int,
        perception: dict | None = None,
        chapters: dict | None = None,
        candidates: dict | None = None,
        visual_notes: dict | None = None,
        chapter_ids: list[str] | None = None,
    ) -> EditIR:
        self.calls.append(
            {
                "chapters": chapters,
                "chapter_ids": chapter_ids,
                "visual_notes": visual_notes,
            }
        )
        return EditIR(
            project_id=project_id,
            revision=revision,
            media_facts=facts,
            sequence=[
                TrimOp(
                    src=facts.src,
                    range=TimeRange(
                        start=RationalTime.from_seconds("1.1", rate=90_000),
                        end=RationalTime.from_seconds("1.8", rate=90_000),
                    ),
                    tag="outline",
                    confidence=0.9,
                )
            ],
        )

    def close(self) -> None:
        pass


def _wait_for_job(client: TestClient, job_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"任务超时: {job_id}")


def test_outline_then_approved_phase_b(
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
        project = client.post("/api/projects", json={"name": "outline"}).json()
        project_id = project["id"]
        ingested = client.post(
            f"/api/projects/{project_id}/ingest",
            json={"src": str(synthetic_video)},
        )
        assert ingested.status_code == 200, ingested.text

        phase_a = client.post(
            f"/api/projects/{project_id}/orchestrate",
            json={
                "goal": "按大纲剪一段",
                "allow_render": False,
                "phase": "outline",
            },
        )
        assert phase_a.status_code == 202, phase_a.text
        job_a = _wait_for_job(client, phase_a.json()["job_id"])
        assert job_a["status"] == "succeeded", job_a

        run_a = client.get(f"/api/runs/{phase_a.json()['run_id']}").json()
        assert run_a["status"] == "awaiting_approval", run_a["answer"]
        assert run_a["phase"] == "outline"
        assert not client.get(
            f"/api/projects/{project_id}/revisions"
        ).json()["revisions"]
        assert not planner.calls

        outline = client.get(f"/api/runs/{run_a['id']}/outline").json()
        assert outline["status"] == "awaiting_approval"
        assert outline["approved"] is False
        chapters = outline["chapters"]
        assert len(chapters) >= 2
        selected = chapters[-1]
        assert selected["id"] == "ch_0002"
        edited_start = RationalTime.model_validate(
            selected["start"]
        ) + RationalTime.from_seconds("0.05", rate=90_000)
        edited_end = RationalTime.model_validate(
            selected["end"]
        ) - RationalTime.from_seconds("0.05", rate=90_000)

        approved = client.post(
            f"/api/runs/{run_a['id']}/approve-outline",
            json={
                "chapters": [
                    {
                        "id": selected["id"],
                        "title": "人工改名",
                        "start": edited_start.model_dump(),
                        "end": edited_end.model_dump(),
                    }
                ],
                "notes": "only the second chapter",
            },
        )
        assert approved.status_code == 202, approved.text
        body = approved.json()
        job_b = _wait_for_job(client, body["job_id"])
        assert job_b["status"] == "succeeded", job_b

        run_b = client.get(f"/api/runs/{body['run_id']}").json()
        assert run_b["status"] == "succeeded", run_b["answer"]
        assert run_b["phase"] == "plan"
        assert run_b["approved_outline_id"] == body["outline_id"]

        assert len(planner.calls) == 1
        call = planner.calls[0]
        assert call["chapter_ids"] == [selected["id"]]
        assert [item["id"] for item in call["chapters"]["chapters"]] == [selected["id"]]
        assert call["chapters"]["chapters"][0]["title"] == "人工改名"
        assert RationalTime.model_validate(
            call["chapters"]["chapters"][0]["start"]
        ) == edited_start
        assert RationalTime.model_validate(
            call["chapters"]["chapters"][0]["end"]
        ) == edited_end
        assert call["visual_notes"] is None

        revisions = client.get(
            f"/api/projects/{project_id}/revisions"
        ).json()["revisions"]
        assert len(revisions) == 1

        approved_outline = client.get(f"/api/runs/{run_b['id']}/outline").json()
        assert approved_outline["approved"] is True
        assert approved_outline["outline_id"] == body["outline_id"]
        assert approved_outline["chapters"][0]["title"] == "人工改名"
