from __future__ import annotations

import hashlib
import time
from pathlib import Path

from fastapi.testclient import TestClient

from smartcut.config import Settings
from smartcut.contracts.effects import FitEffect
from smartcut.contracts.edit_ir import (
    AudioSpec,
    EditIR,
    ExportSpec,
    TimeRange,
    TrimOp,
)
from smartcut.contracts.time import RationalTime
from smartcut.contracts.transcript import Transcript, TranscriptSegment, Word
from smartcut.runner.execute import ToolCallExecutor
from smartcut.runner.paths import Workspace
from smartcut.server.main import create_app


class FakeTranscriber:
    name = "fake"
    degraded = False

    def transcribe(self, audio_path: Path) -> Transcript:
        words = [
            Word(
                text="hello",
                start=RationalTime.from_seconds("0.4", rate=90_000),
                end=RationalTime.from_seconds("0.8", rate=90_000),
            ),
            Word(
                text="world",
                start=RationalTime.from_seconds("0.9", rate=90_000),
                end=RationalTime.from_seconds("1.3", rate=90_000),
            ),
        ]
        segment = TranscriptSegment(
            id=0,
            text="hello world",
            start=words[0].start,
            end=words[-1].end,
            words=words,
        )
        return Transcript(language="en", words=words, segments=[segment], degraded=False)


class FakePlannerP1:
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
                        start=RationalTime.from_seconds("0.2", rate=90_000),
                        end=RationalTime.from_seconds("1.8", rate=90_000),
                    ),
                    tag="packaged",
                    confidence=0.95,
                )
            ],
            effects=[FitEffect(anchor="center")],
            audio=AudioSpec(loudness="-14 LUFS"),
            export=ExportSpec(burn_captions=True),
        )

    def close(self) -> None:
        pass


def _wait_for_job(client: TestClient, job_id: str, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.1)
    raise AssertionError(f"任务超时: {job_id}")


def test_orchestrator_p1_fit_captions_loudness(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    app = create_app(
        settings,
        transcriber=FakeTranscriber(),
        planner=FakePlannerP1(),
    )
    source_hash = hashlib.sha256(synthetic_video.read_bytes()).hexdigest()

    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "p1"}).json()
        project_id = project["id"]
        ingest = client.post(
            f"/api/projects/{project_id}/ingest",
            json={"src": str(synthetic_video)},
        )
        assert ingest.status_code == 200, ingest.text

        response = client.post(
            f"/api/projects/{project_id}/orchestrate",
            json={"goal": "输出 9:16 竖版，烧录字幕，响度归一 -14 LUFS"},
        )
        assert response.status_code == 202, response.text
        job = _wait_for_job(client, response.json()["job_id"])
        assert job["status"] == "succeeded", job

        run = client.get(f"/api/runs/{response.json()['run_id']}").json()
        assert run["status"] == "succeeded", run["answer"]
        assert "字幕" in run["answer"]
        agents = {item["agent"] for item in run["ledger"]}
        assert {
            "frames",
            "scenes",
            "quality",
            "faces",
            "contactsheet",
            "chapters",
            "plan",
            "compile",
            "render",
            "verify",
        } <= agents

        artifacts = set(run["artifacts"])
        assert "renders/r1/output.mp4" in artifacts
        assert "renders/r1/captions.srt" in artifacts
        assert "renders/r1/captions.vtt" in artifacts

        workspace_dir = Path(project["workspace_dir"])
        assert (workspace_dir / "renders/r1/output.mp4").exists()
        assert (workspace_dir / "renders/r1/captions.srt").exists()
        assert "hello" in (workspace_dir / "renders/r1/captions.srt").read_text(encoding="utf-8")

        probe = ToolCallExecutor(settings, Workspace(workspace_dir))
        final = probe.probe("renders/r1/output.mp4")
        assert (final.width, final.height) == (1080, 1920)
        assert final.has_audio
        assert abs(final.duration.to_float() - 1.6) <= 0.25
        assert hashlib.sha256(synthetic_video.read_bytes()).hexdigest() == source_hash
