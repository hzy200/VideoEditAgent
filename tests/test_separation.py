from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from smartcut.cancel import CancellationToken
from smartcut.config import Settings
from smartcut.errors import RunCancelledError
from smartcut.perception.transcribe import NullTranscriber
from smartcut.runner.paths import Workspace
from smartcut.runner.separation import (
    Separator,
    build_argv,
    build_separator,
    resolve_command,
)
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.main import create_app
from smartcut.server.services import Services


FAKE_DEMUCS = r'''
import pathlib, shutil, sys
args = sys.argv[1:]
out = pathlib.Path(args[args.index("-o") + 1])
source = pathlib.Path(args[-1])
model = args[args.index("-n") + 1] if "-n" in args else "htdemucs"
target = out / model / source.stem
target.mkdir(parents=True, exist_ok=True)
for name in ("vocals", "no_vocals"):
    shutil.copyfile(source, target / f"{name}.wav")
marker = pathlib.Path(__file__).with_name("calls.txt")
marker.write_text(marker.read_text() + "x" if marker.exists() else "x", encoding="utf-8")
'''


def _settings(workspace: Workspace, command: str | None = None) -> Settings:
    import shutil

    return Settings(
        workspace_root=workspace.root,
        ffmpeg_bin=shutil.which("ffmpeg"),
        ffprobe_bin=shutil.which("ffprobe"),
        hardware_encoding="off",
        demucs_command=command,
    )


def test_demucs_argv_and_availability(tmp_path: Path, workspace: Workspace) -> None:
    settings = _settings(workspace, command=f'"{sys.executable}" fake.py')
    assert resolve_command(settings) == [sys.executable, "fake.py"]
    separator = build_separator(settings, workspace)
    assert isinstance(separator, Separator)
    assert separator.name == "demucs"
    assert separator.available() is True

    argv = build_argv(settings, tmp_path / "in.wav", tmp_path / "out")
    assert "--two-stems=vocals" in argv
    assert "htdemucs" in argv
    assert argv[-1].endswith("in.wav")

    four = build_argv(settings, tmp_path / "in.wav", tmp_path / "out", stems=4)
    assert "--two-stems=vocals" not in four


def test_separation_registers_stems_and_uses_cache(
    tmp_path: Path, workspace: Workspace, synthetic_video: Path
) -> None:
    script = tmp_path / "fake_demucs.py"
    script.write_text(FAKE_DEMUCS, encoding="utf-8")
    settings = _settings(
        workspace,
        command=f'"{sys.executable}" "{script}"',
    )
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(
        settings,
        db,
        jobs,
        transcriber=NullTranscriber(),
    )
    try:
        project = services.create_project("sep")
        project_id = project["id"]
        services.ingest(project_id, str(synthetic_video))

        first = services.separate_audio(project_id)
        assert first["cached"] is False
        assert set(first["stems"][0]) >= {"id", "name", "src", "facts"}
        assets = services.list_audio_assets(project_id)
        assert {asset["name"] for asset in assets} == {"人声", "伴奏"}
        assert (tmp_path / "calls.txt").read_text(encoding="utf-8") == "x"

        # 缓存命中不应再依赖 Demucs 是否仍可用。
        services.settings.demucs_command = None
        second = services.separate_audio(project_id)
        assert second["cached"] is True
        assert (tmp_path / "calls.txt").read_text(encoding="utf-8") == "x"
        assert len(services.list_audio_assets(project_id)) == 2
        assert first["metadata"]["separator"] == "demucs"
        assert first["metadata"]["stems"] == 2
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_separator_cancellation(tmp_path: Path, workspace: Workspace, synthetic_video: Path) -> None:
    script = tmp_path / "slow_demucs.py"
    script.write_text(
        "import time\nwhile True:\n    time.sleep(0.5)\n",
        encoding="utf-8",
    )
    settings = _settings(workspace, command=f'"{sys.executable}" "{script}"')
    separator = build_separator(settings, workspace)
    token = CancellationToken()
    token.cancel()
    with pytest.raises(RunCancelledError):
        separator.separate(
            synthetic_video,
            tmp_path / "out",
            cancel_token=token,
        )


def test_separate_job_kind(workspace: Workspace, synthetic_video: Path, tmp_path: Path) -> None:
    script = tmp_path / "fake_job_demucs.py"
    script.write_text(FAKE_DEMUCS, encoding="utf-8")
    settings = _settings(workspace, command=f'"{sys.executable}" "{script}"')
    app = create_app(settings, transcriber=NullTranscriber())
    with TestClient(app) as client:
        project_id = client.post("/api/projects", json={"name": "job"}).json()["id"]
        client.post(
            f"/api/projects/{project_id}/ingest",
            json={"src": str(synthetic_video)},
        )
        response = client.post(
            f"/api/projects/{project_id}/separate", json={}
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        deadline = time.time() + 30
        job = None
        while time.time() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        assert job is not None and job["status"] == "succeeded", job
        assert job["kind"] == "separate"
        assert {item["name"] for item in job["result"]["stems"]} == {"人声", "伴奏"}
