"""真实 GPU 联调：Demucs 人声分离。

默认跳过；显式运行：

    SMARTCUT_LIVE_GPU=1 python -m pytest -q -m live tests/test_gpu_live.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from smartcut.config import load_settings
from smartcut.perception.transcribe import NullTranscriber
from smartcut.runner.separation import available as demucs_available
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
SPEECH = SAMPLES / "李世民对长孙无忌是真爱啊.mp4"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("SMARTCUT_LIVE_GPU") != "1",
        reason="设置 SMARTCUT_LIVE_GPU=1 才运行真实 GPU 分离测试",
    ),
]


def _settings(tmp_path: Path):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("缺少 ffmpeg/ffprobe")
    return load_settings(
        workspace_root=tmp_path / "ws",
        ffmpeg_bin=ffmpeg,
        ffprobe_bin=ffprobe,
        demucs_command=None,
        demucs_model="htdemucs",
    )


def test_demucs_probe_reports_cuda() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "smartcut.separation_worker", "--probe"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        pytest.skip(f"Demucs probe 不可用: {result.stderr[-500:]}")
    probe = json.loads(result.stdout.strip().splitlines()[-1])
    assert "cuda" in str(probe.get("device", "")) or probe.get("cuda_available")
    assert probe.get("gpu")


def test_demucs_separates_short_clip_on_gpu(tmp_path: Path) -> None:
    if not SPEECH.is_file():
        pytest.skip("缺少样本视频")
    settings = _settings(tmp_path)
    if not demucs_available(settings):
        pytest.skip("Demucs 不可用")

    clip = tmp_path / "sep_20s.mp4"
    subprocess.run(
        [
            settings.ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-ss",
            "0",
            "-t",
            "20",
            "-i",
            str(SPEECH),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "28",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(clip),
        ],
        check=True,
        capture_output=True,
    )

    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(
        settings, db, jobs, transcriber=NullTranscriber()
    )
    try:
        project = services.create_project("gpu-demucs")
        services.ingest(project["id"], str(clip))
        result = services.separate_audio(project["id"], stems=2)
        assert result["stems_requested"] == 2
        names = {item["name"] for item in result["stems"]}
        assert {"人声", "伴奏"} <= names
        metadata = result.get("metadata") or {}
        assert metadata.get("device", "").startswith("cuda"), metadata
        assert metadata.get("gpu")
        workspace = services.project_workspace(project["id"])
        for asset in result["stems"]:
            assert workspace.resolve(asset["src"]).exists()
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
