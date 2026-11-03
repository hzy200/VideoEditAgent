from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from smartcut.config import Settings
from smartcut.contracts.transcode import (
    AudioOutput,
    MetadataOptions,
    SubtitleOutput,
    TranscodeProfile,
    TranscodeRequest,
)
from smartcut.perception.transcribe import NullTranscriber
from smartcut.runner.paths import Workspace
from smartcut.runner.tools.base import ToolContext
from smartcut.runner.tools.transcode import TranscodeArgs, build_argv
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.main import create_app
from smartcut.server.services import Services


def _ctx(settings: Settings, workspace: Workspace) -> ToolContext:
    return ToolContext(settings=settings, workspace=workspace)


def test_contract_rejects_incompatible_container_codec() -> None:
    with pytest.raises(ValueError):
        TranscodeProfile(container="mp4", video_codec="prores_ks")
    with pytest.raises(ValueError):
        TranscodeProfile(container="mp4", video_codec="copy", width=1280)
    with pytest.raises(ValueError):
        TranscodeProfile(
            container="mp4",
            video_codec="libx264",
            keep_10bit=True,
        )


def test_build_argv_for_hevc_with_audio_and_subtitles(
    settings: Settings,
    workspace: Workspace,
    tmp_path: Path,
) -> None:
    (workspace.root / "media").mkdir(parents=True, exist_ok=True)
    (workspace.root / "media" / "a.mp4").write_bytes(b"x")
    (workspace.root / "audio").mkdir(parents=True, exist_ok=True)
    (workspace.root / "audio" / "bgm.wav").write_bytes(b"x")
    (workspace.root / "media" / "sub.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")

    profile = TranscodeProfile(
        container="mp4",
        video_codec="libx265",
        width=1280,
        height=720,
        fps="30",
        pix_fmt="yuv420p10le",
        keep_10bit=True,
        audio_tracks=[
            AudioOutput(codec="aac", bitrate="192k", channels=2, language="zho"),
            AudioOutput(src="audio/bgm.wav", codec="aac", gain_db=-6, language="eng"),
        ],
        subtitle_tracks=[
            SubtitleOutput(src="media/sub.srt", codec="mov_text", language="zho")
        ],
        metadata=MetadataOptions(title="demo", extra={"copyright": "x"}),
    )
    argv = build_argv(
        TranscodeArgs(src="media/a.mp4", dst="exports/a.mp4", profile=profile),
        _ctx(settings, workspace),
    )
    joined = " ".join(argv)
    assert "-c:v libx265" in joined
    assert "-pix_fmt yuv420p10le" in joined
    assert "-fps_mode cfr" in joined
    assert "-map 0:a:0?" in joined
    assert "-map 1:a:0?" in joined
    assert "-map 2:s:0?" in joined
    assert "-c:s:0 mov_text" in joined
    assert "-metadata title=demo" in joined
    assert "use_metadata_tags" in joined
    assert "sidechain" not in joined


def test_build_argv_maps_multiple_source_audio_streams(
    settings: Settings,
    workspace: Workspace,
) -> None:
    (workspace.root / "media").mkdir(parents=True, exist_ok=True)
    (workspace.root / "media" / "multi.mp4").write_bytes(b"x")
    profile = TranscodeProfile(
        container="mkv",
        video_codec="copy",
        fps_mode="passthrough",
        audio_tracks=[
            AudioOutput(stream_index=0, codec="aac", language="zho"),
            AudioOutput(stream_index=1, codec="ac3", language="eng"),
        ],
    )
    argv = build_argv(
        TranscodeArgs(src="media/multi.mp4", dst="exports/multi.mkv", profile=profile),
        _ctx(settings, workspace),
    )
    joined = " ".join(argv)
    assert "-map 0:a:0?" in joined
    assert "-map 0:a:1?" in joined
    assert "-c:a:0 aac" in joined
    assert "-c:a:1 ac3" in joined
    assert "-metadata:s:a:0 language=zho" in joined
    assert "-metadata:s:a:1 language=eng" in joined


def test_build_argv_rejects_scale_in_copy_mode() -> None:
    with pytest.raises(ValueError):
        TranscodeProfile(video_codec="copy", width=100, height=100, fps_mode="passthrough")


def _services(settings: Settings) -> tuple[Services, JobService, Database]:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    return Services(settings, db, jobs, transcriber=NullTranscriber()), jobs, db


def test_transcode_api_submits_job(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    app = create_app(settings, transcriber=NullTranscriber())
    with TestClient(app) as client:
        project_id = client.post("/api/projects", json={"name": "api-tx"}).json()["id"]
        assert client.post(
            f"/api/projects/{project_id}/ingest",
            json={"src": str(synthetic_video)},
        ).status_code == 200
        response = client.post(
            f"/api/projects/{project_id}/transcode",
            json={
                "profile": {
                    "container": "mp4",
                    "video_codec": "libx264",
                    "preset": "veryfast",
                    "crf": 30,
                }
            },
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        deadline = time.time() + 60
        job = {}
        while time.time() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        assert job["status"] == "succeeded", job
        assert job["result"]["output_path"].endswith(".mp4")


def test_transcode_service_writes_and_caches(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("tx")["id"]
        services.ingest(project_id, str(synthetic_video))
        request = TranscodeRequest(
            profile=TranscodeProfile(
                container="mkv",
                video_codec="libx264",
                preset="veryfast",
                crf=28,
                fps="30",
                audio_tracks=[AudioOutput(codec="aac", bitrate="96k")],
            )
        )
        first = services.transcode(project_id, request)
        assert first["cached"] is False
        assert first["output_path"].endswith(".mkv")
        output = Path(first["output_abs"])
        assert output.is_file()
        assert output.stat().st_size > 0
        assert first["actual_video_codec"] == "h264"

        second = services.transcode(project_id, request)
        assert second["cached"] is True
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()
