from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from smartcut.config import Settings
from smartcut.perception.transcribe import NullTranscriber
from smartcut.runner.execute import ToolCallExecutor
from smartcut.server.main import create_app
from smartcut.workspace import WorkspaceManager


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wait_for_job(client: TestClient, job_id: str, timeout: float = 60.0) -> dict:
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
                "tag": "keep",
                "confidence": 0.95,
            }
        ],
        "effects": [],
        "audio": {"loudness": None, "remove_filler": False, "bgm": None},
        "export": {"preset": "mp4_h264", "burn_captions": False, "cut_mode": "reencode"},
    }


def test_p0_end_to_end(settings: Settings, synthetic_video: Path) -> None:
    # 关闭离线规则兜底，验证“无 Key 且无兜底”时仍可用手工 IR 完成 P0 闭环。
    settings = settings.model_copy(update={"planner_offline_fallback": False})
    app = create_app(settings, transcriber=NullTranscriber())
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}

        created = client.post("/api/projects", json={"name": "demo"})
        assert created.status_code == 200, created.text
        project = created.json()
        project_id = project["id"]
        workspace_dir = Path(project["workspace_dir"])
        assert workspace_dir == (settings.workspace_root / "projects" / project_id).resolve()
        source_hash = _sha256(synthetic_video)

        ingested = client.post(
            f"/api/projects/{project_id}/ingest",
            json={"src": str(synthetic_video)},
        )
        assert ingested.status_code == 200, ingested.text
        facts = ingested.json()["facts"]
        assert facts["fps"] == "30/1"
        assert facts["has_audio"] is True
        assert facts["src"].startswith("media/")
        assert (workspace_dir / facts["src"]).exists()
        assert (workspace_dir / facts["src"]).resolve() != synthetic_video.resolve()
        # 导入只读源文件，副本在工作区内。
        assert _sha256(synthetic_video) == source_hash

        # 未配置 Key 且关闭离线兜底时，规划接口明确返回 503，不阻塞手工闭环。
        planned = client.post(
            f"/api/projects/{project_id}/plan",
            json={"goal": "keep everything"},
        )
        assert planned.status_code == 503

        revised = client.post(
            f"/api/projects/{project_id}/revisions",
            json={"ir": _manual_ir(project_id, facts)},
        )
        assert revised.status_code == 200, revised.text
        revision = revised.json()["revision"]
        assert revision == 1

        dry_run = client.post(
            f"/api/projects/{project_id}/render",
            json={"revision": revision, "dry_run": True},
        )
        assert dry_run.status_code == 200, dry_run.text
        assert [call["tool"] for call in dry_run.json()["tool_calls"]] == [
            "probe",
            "cut",
            "verify",
        ]

        rendered = client.post(
            f"/api/projects/{project_id}/render",
            json={"revision": revision, "dry_run": False},
        )
        assert rendered.status_code == 200, rendered.text
        job = _wait_for_job(client, rendered.json()["job_id"])
        assert job["status"] == "succeeded", job
        result = job["result"]
        assert result["output_path"] == "renders/r1/output.mp4"
        assert Path(result["output_abs"]).exists()
        assert result["facts"] is not None

        workspace = WorkspaceManager(settings.workspace_root).get(project_id)
        probe = ToolCallExecutor(settings, workspace)
        final = probe.probe(result["output_path"])
        assert abs(final.duration.to_float() - 1.0) <= 0.15
        assert final.has_audio

        artifacts = client.get(f"/api/projects/{project_id}/artifacts")
        assert artifacts.status_code == 200
        assert result["output_path"] in artifacts.json()["artifacts"]
        assert _sha256(synthetic_video) == source_hash

        revisions = client.get(f"/api/projects/{project_id}/revisions")
        assert revisions.status_code == 200
        assert len(revisions.json()["revisions"]) == 1

        changed = _manual_ir(project_id, facts)
        changed["sequence"][0]["range"]["end"] = {"value": 108_000, "rate": 90_000}
        second = client.post(
            f"/api/projects/{project_id}/revisions",
            json={"ir": changed},
        )
        assert second.status_code == 200, second.text
        assert second.json()["revision"] == 2
        diff = client.get(f"/api/projects/{project_id}/revisions/2/diff/1")
        assert diff.status_code == 200, diff.text
        payload = diff.json()
        assert payload["added_count"] == 1
        assert payload["removed_count"] == 1
        assert payload["unchanged_count"] == 0
        same = client.get(f"/api/projects/{project_id}/revisions/2/diff/2")
        assert same.status_code == 422

        transcribed = client.post(f"/api/projects/{project_id}/transcribe")
        assert transcribed.status_code == 202, transcribed.text
        transcribe_job = _wait_for_job(client, transcribed.json()["job_id"])
        assert transcribe_job["status"] == "succeeded", transcribe_job
        assert transcribe_job["result"]["degraded"] is True

        missing = client.get(f"/api/projects/{project_id}/revisions/999")
        assert missing.status_code == 404


def test_manual_subtitles_and_audio_survive_revision_roundtrip(
    settings: Settings, synthetic_video: Path
) -> None:
    app = create_app(settings, transcriber=NullTranscriber())
    with TestClient(app) as client:
        project_id = client.post("/api/projects", json={"name": "manual"}).json()["id"]
        facts = client.post(
            f"/api/projects/{project_id}/ingest",
            json={"src": str(synthetic_video)},
        ).json()["facts"]

        payload = _manual_ir(project_id, facts)
        payload["audio"].update({"volume_db": -3.5, "pitch_semitones": 4.0})
        payload["subtitles"] = [
            {
                "start": {"value": 18_000, "rate": 90_000},
                "end": {"value": 72_000, "rate": 90_000},
                "text": "手动字幕",
            }
        ]
        created = client.post(
            f"/api/projects/{project_id}/revisions", json={"ir": payload}
        )
        assert created.status_code == 200, created.text
        assert created.json()["revision"] == 1

        services = app.state.services
        stored = services.get_revision(project_id, 1)
        assert stored.subtitles is not None
        assert stored.subtitles[0].text == "手动字幕"
        assert stored.audio.volume_db == pytest.approx(-3.5)
        assert stored.audio.pitch_semitones == pytest.approx(4.0)

        preview = services.caption_preview(project_id, 1)
        assert [cue["text"] for cue in preview["captions"]] == ["手动字幕"]
        assert "手动字幕" in preview["srt"]

        dry = client.post(
            f"/api/projects/{project_id}/render",
            json={"revision": 1, "dry_run": True},
        )
        assert dry.status_code == 200, dry.text
        calls = dry.json()["tool_calls"]
        assert [call["tool"] for call in calls] == [
            "probe",
            "cut",
            "postprocess",
            "verify",
        ]
        post = next(call for call in calls if call["tool"] == "postprocess")
        assert post["args"]["volume_db"] == pytest.approx(-3.5)
        assert post["args"]["pitch_semitones"] == pytest.approx(4.0)


def test_upload_is_written_into_task_workspace(settings: Settings, synthetic_video: Path) -> None:
    app = create_app(settings, transcriber=NullTranscriber())
    source_hash = _sha256(synthetic_video)
    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "upload"}).json()
        project_id = project["id"]
        response = client.post(
            f"/api/projects/{project_id}/upload",
            files={"file": ("source.mp4", synthetic_video.read_bytes(), "video/mp4")},
        )
        assert response.status_code == 200, response.text
        asset = response.json()
        assert asset["src"].startswith("media/")

        workspace = Path(client.get(f"/api/projects/{project_id}").json()["workspace_dir"])
        copied = workspace / asset["src"]
        assert copied.exists()
        assert copied.resolve() != synthetic_video.resolve()
        assert _sha256(synthetic_video) == source_hash
