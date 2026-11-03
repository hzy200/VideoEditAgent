"""真实 DeepSeek 联调测试。

默认跳过；需要显式开启并使用根目录 .env 中的 DEEPSEEK_* 配置：

    SMARTCUT_LIVE_DEEPSEEK=1 python -m pytest -q -m live
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from smartcut.config import load_settings
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.perception.transcribe import NullTranscriber
from smartcut.planner.client import DeepSeekPlanner
from smartcut.server.main import create_app

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("SMARTCUT_LIVE_DEEPSEEK") != "1",
        reason="设置 SMARTCUT_LIVE_DEEPSEEK=1 才运行真实 API 测试",
    ),
]


def test_live_deepseek_planner_returns_valid_ir() -> None:
    settings = load_settings()
    if not settings.deepseek_api_key:
        pytest.skip("未配置 DEEPSEEK_API_KEY")

    facts = MediaFacts(
        src="media/live_demo.mp4",
        duration=RationalTime.from_seconds("10", rate=90_000),
        fps="30/1",
        video=StreamFacts(
            codec_type="video",
            width=1280,
            height=720,
            avg_frame_rate="30/1",
            r_frame_rate="30/1",
        ),
        audio=StreamFacts(
            codec_type="audio",
            channels=2,
            sample_rate=48_000,
        ),
    )

    planner = DeepSeekPlanner(settings)
    try:
        ir = planner.propose(
            facts,
            None,
            "保留前 2 秒，不要任何视觉效果",
            project_id="p_live",
            revision=1,
        )
    finally:
        planner.close()

    assert ir.project_id == "p_live"
    assert ir.revision == 1
    assert ir.media_facts.src == facts.src
    assert len(ir.sequence) >= 1
    assert ir.export.preset == "mp4_h264"
    assert 0.0 < ir.total_duration().to_float() <= 10.0


def _wait_job(client: TestClient, job_id: str, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.25)
    raise AssertionError(f"live orchestration job 超时: {job_id}")


def test_live_orchestrator_full_pipeline(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("缺少 ffmpeg/ffprobe")

    settings = load_settings()
    if not settings.deepseek_api_key:
        pytest.skip("未配置 DEEPSEEK_API_KEY")
    settings = load_settings(
        workspace_root=tmp_path / "ws",
        ffmpeg_bin=ffmpeg,
        ffprobe_bin=ffprobe,
    )

    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=30:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
        capture_output=True,
    )

    app = create_app(settings, transcriber=NullTranscriber())
    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "live"}).json()
        project_id = project["id"]
        ingest = client.post(
            f"/api/projects/{project_id}/ingest",
            json={"src": str(source)},
        )
        assert ingest.status_code == 200, ingest.text

        response = client.post(
            f"/api/projects/{project_id}/orchestrate",
            json={
                "goal": "保留整段素材，去掉明显的空白片段；输出一个短视频",
                "allow_render": True,
            },
        )
        assert response.status_code == 202, response.text
        job = _wait_job(client, response.json()["job_id"])
        assert job["status"] == "succeeded", job

        run = client.get(f"/api/runs/{response.json()['run_id']}").json()
        agents = {item["agent"] for item in run["ledger"]}
        assert {
            "analyze",
            "frames",
            "scenes",
            "quality",
            "faces",
            "contactsheet",
            "transcribe",
            "plan",
            "compile",
            "render",
            "verify",
        } <= agents
        assert run["answer"]
        assert run["status"] == "succeeded", run["answer"]
        assert run["artifacts"], run["answer"]

