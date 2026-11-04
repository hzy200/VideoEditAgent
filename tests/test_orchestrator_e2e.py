from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.time import RationalTime
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.main import create_app


class FakePlanner:
    """可预测的规划器，避免测试依赖真实 API。"""

    available = True

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
        return EditIR(
            project_id=project_id,
            revision=revision,
            media_facts=facts,
            sequence=[
                TrimOp(
                    src=facts.src,
                    range=TimeRange(
                        start=RationalTime.from_seconds("0.5", rate=90_000),
                        end=RationalTime.from_seconds("1.5", rate=90_000),
                    ),
                    tag="fake",
                    confidence=0.9,
                )
            ],
        )

    def close(self) -> None:
        pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wait_for_job(client: TestClient, job_id: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"任务超时: {job_id}")


def _manual_ir(project_id: str, facts: dict) -> dict:
    return {
        "schema": "editir/v1",
        "project_id": project_id,
        "revision": 0,
        "media_facts": facts,
        "sequence": [
            {
                "op": "trim",
                "src": facts["src"],
                "range": {
                    "start": {"value": 45_000, "rate": 90_000},
                    "end": {"value": 135_000, "rate": 90_000},
                },
                "tag": "manual",
                "confidence": 0.9,
            }
        ],
        "effects": [],
        "audio": {"loudness": None, "remove_filler": False, "bgm": None},
        "export": {"preset": "mp4_h264", "burn_captions": False, "cut_mode": "reencode"},
    }


def _setup(client: TestClient, synthetic_video: Path) -> tuple[str, dict, Path]:
    project = client.post("/api/projects", json={"name": "orchestrator"}).json()
    project_id = project["id"]
    ingested = client.post(
        f"/api/projects/{project_id}/ingest",
        json={"src": str(synthetic_video)},
    )
    assert ingested.status_code == 200, ingested.text
    facts = ingested.json()["facts"]
    workspace = Path(project["workspace_dir"])
    return project_id, facts, workspace


class ExplodingTranscriber:
    name = "exploding"
    degraded = False

    def transcribe(self, audio_path: Path):
        raise RuntimeError("whisper boom")


def test_transcribe_failure_degrades_and_plan_still_runs(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    app = create_app(
        settings,
        transcriber=ExplodingTranscriber(),
        planner=FakePlanner(),
    )
    with TestClient(app) as client:
        project_id, _facts, _workspace = _setup(client, synthetic_video)
        response = client.post(
            f"/api/projects/{project_id}/orchestrate",
            json={"goal": "降级规划", "allow_render": False},
        )
        assert response.status_code == 202, response.text
        job = _wait_for_job(client, response.json()["job_id"])
        assert job["status"] == "succeeded", job

        run = client.get(f"/api/runs/{response.json()['run_id']}").json()
        transcribe = next(
            item for item in run["ledger"] if item["agent"] == "transcribe"
        )
        assert transcribe["status"] == "succeeded"
        assert transcribe["output"]["degraded"] is True
        assert "whisper boom" in " ".join(run["warnings"])
        revisions = client.get(
            f"/api/projects/{project_id}/revisions"
        ).json()["revisions"]
        assert revisions


def test_orchestrator_full_pipeline(settings: Settings, synthetic_video: Path) -> None:
    app = create_app(
        settings,
        transcriber=NullTranscriber(),
        planner=FakePlanner(),
    )
    source_hash = _sha256(synthetic_video)

    with TestClient(app) as client:
        project_id, facts, workspace = _setup(client, synthetic_video)
        response = client.post(
            f"/api/projects/{project_id}/orchestrate",
            json={"goal": "剪成 1 秒竖版片段", "allow_render": True},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        job = _wait_for_job(client, response.json()["job_id"])
        assert job["status"] == "succeeded", job

        run = client.get(f"/api/runs/{run_id}").json()
        assert run["status"] == "succeeded", run
        assert run["answer"]
        assert "产出" in run["answer"]
        assert "renders/r1/output.mp4" in run["artifacts"]
        assert (workspace / "renders/r1/output.mp4").exists()

        agents = {item["agent"] for item in run["ledger"]}
        assert agents == {
            "analyze",
            "frames",
            "scenes",
            "quality",
            "faces",
            "contactsheet",
            "chapters",
            "candidates",
            "transcribe",
            "plan",
            "audio",
            "critic",
            "qa",
            "replan",
            "compile",
            "render",
            "verify",
        }
        assert all(item["status"] == "succeeded" for item in run["ledger"])
        assert run["confidence"] >= 0.9
        metrics = run["metrics"]
        assert len(metrics["tasks"]) == len(run["ledger"])
        assert metrics["totals"]["duration_ms"] >= 0
        assert 0.0 <= metrics["totals"]["cache_hit_rate"] <= 1.0
        assert metrics["totals"]["queue_wait_ms"] >= 0
        assert metrics["encoders"]
        assert any(task["agent"] == "render" for task in metrics["tasks"])
        assert (workspace / "tmp/runs" / run_id / "result.json").exists()
        stored = json.loads(
            (workspace / "tmp/runs" / run_id / "result.json").read_text(encoding="utf-8")
        )
        assert stored["metrics"]["totals"]["duration_ms"] >= 0
        assert _sha256(synthetic_video) == source_hash

        listed = client.get(f"/api/projects/{project_id}/runs")
        assert listed.status_code == 200
        summary = listed.json()["runs"][0]
        assert summary["id"] == run_id
        assert summary["artifact_count"] >= 1
        assert "ledger" not in summary

        candidates = client.get(f"/api/projects/{project_id}/candidates")
        assert candidates.status_code == 200
        assert candidates.json()["candidates"]
        searched = client.get(
            f"/api/projects/{project_id}/candidates/search",
            params={"q": "nothing-matches-this"},
        )
        assert searched.status_code == 200
        assert searched.json()["count"] >= 1


def test_orchestrator_manual_ir_without_api_key(
    settings: Settings, synthetic_video: Path
) -> None:
    app = create_app(settings, transcriber=NullTranscriber())
    with TestClient(app) as client:
        project_id, facts, workspace = _setup(client, synthetic_video)
        response = client.post(
            f"/api/projects/{project_id}/orchestrate",
            json={
                "goal": "手工剪一段",
                "allow_render": True,
                "manual_ir": _manual_ir(project_id, facts),
            },
        )
        assert response.status_code == 202, response.text
        job = _wait_for_job(client, response.json()["job_id"])
        assert job["status"] == "succeeded", job
        run = client.get(f"/api/runs/{response.json()['run_id']}").json()
        assert run["status"] == "succeeded", run
        assert (workspace / "renders/r1/output.mp4").exists()


def test_orchestrator_without_planner_or_manual_ir(
    settings: Settings, synthetic_video: Path
) -> None:
    settings = settings.model_copy(update={"planner_offline_fallback": False})
    app = create_app(settings, transcriber=NullTranscriber())
    with TestClient(app) as client:
        project_id, _facts, _workspace = _setup(client, synthetic_video)
        response = client.post(
            f"/api/projects/{project_id}/orchestrate",
            json={"goal": "分析一下", "allow_render": True},
        )
        assert response.status_code == 202, response.text
        job = _wait_for_job(client, response.json()["job_id"])
        assert job["status"] == "succeeded", job

        run = client.get(f"/api/runs/{response.json()['run_id']}").json()
        assert run["status"] == "succeeded"
        assert "未生成 EditIR" in run["answer"]
        assert not any(item.startswith("renders/") for item in run["artifacts"])
        agents = {item["agent"] for item in run["ledger"]}
        assert {
            "analyze",
            "frames",
            "scenes",
            "quality",
            "faces",
            "contactsheet",
            "chapters",
            "candidates",
            "transcribe",
        } <= agents
        assert "plan" not in agents
        assert "critic" not in agents


def test_orchestrator_offline_rule_planner_runs_full_dag(
    settings: Settings, synthetic_video: Path
) -> None:
    app = create_app(settings, transcriber=NullTranscriber())
    with TestClient(app) as client:
        project_id, _facts, _workspace = _setup(client, synthetic_video)
        response = client.post(
            f"/api/projects/{project_id}/orchestrate",
            json={"goal": "保留前 1 秒", "allow_render": False},
        )
        assert response.status_code == 202, response.text
        job = _wait_for_job(client, response.json()["job_id"])
        assert job["status"] == "succeeded", job

        run = client.get(f"/api/runs/{response.json()['run_id']}").json()
        assert run["status"] == "succeeded", run
        ledger = {item["agent"]: item for item in run["ledger"]}
        assert {"plan", "audio", "critic", "qa"} <= set(ledger)
        assert ledger["plan"]["output"]["source"] == "rule"
        assert "规则规划（离线" in run["answer"]


def test_orchestrator_allow_render_false(
    settings: Settings, synthetic_video: Path
) -> None:
    app = create_app(settings, transcriber=NullTranscriber(), planner=FakePlanner())
    with TestClient(app) as client:
        project_id, _facts, _workspace = _setup(client, synthetic_video)
        response = client.post(
            f"/api/projects/{project_id}/orchestrate",
            json={"goal": "只做分析", "allow_render": False},
        )
        assert response.status_code == 202, response.text
        job = _wait_for_job(client, response.json()["job_id"])
        assert job["status"] == "succeeded", job

        run = client.get(f"/api/runs/{response.json()['run_id']}").json()
        assert run["status"] == "succeeded"
        assert "未执行渲染" in run["answer"]
        assert not any(item.startswith("renders/") for item in run["artifacts"])
        agents = {item["agent"] for item in run["ledger"]}
        assert "plan" in agents
        assert "audio" in agents
        assert "critic" in agents
        assert "qa" in agents
        assert "render" not in agents
        assert "compile" not in agents
