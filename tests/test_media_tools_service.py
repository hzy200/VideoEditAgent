from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from smartcut.config import Settings
from smartcut.contracts.media_ops import RepairRequest
from smartcut.contracts.transcode import (
    AudioOutput,
    TranscodeProfile,
    TranscodeRequest,
)
from smartcut.errors import ToolExecutionError
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _services(settings: Settings) -> tuple[Services, JobService, Database]:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    return Services(settings, db, jobs, transcriber=NullTranscriber()), jobs, db


def _ingested(
    services: Services, source: Path
) -> str:
    project_id = services.create_project("media-tools")["id"]
    services.ingest(project_id, str(source))
    return project_id


def test_repair_auto_remux_and_cache(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = _ingested(services, synthetic_video)
        diagnosis = services.diagnose_media(project_id)
        assert diagnosis.probe_ok is True
        first = services.repair_media(
            project_id, RepairRequest(strategy="auto")
        )
        assert first["cached"] is False
        output = Path(first["output_abs"])
        assert output.is_file()
        assert output.stat().st_size > 0
        assert first["output_path"].startswith("repairs/")

        second = services.repair_media(
            project_id, RepairRequest(strategy="auto")
        )
        assert second["cached"] is True
        assert Path(second["output_abs"]).is_file()
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_import_raw_media_supports_unopenable_file(
    settings: Settings,
    tmp_path: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("raw-import")["id"]
        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"not a media file")
        imported = services.import_raw_media(project_id, str(broken))
        assert imported["src"].startswith("media/")
        workspace = services.workspaces.get(project_id)
        assert (workspace.root / imported["src"]).is_file()
        diagnosis = services.diagnose_media(project_id, imported["src"])
        assert diagnosis.probe_ok is False
        assert diagnosis.issues[0].code == "unopenable"
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_repair_real_audio_desync(
    settings: Settings,
    ffmpeg_bin: str,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("sync-repair")["id"]
        workspace = services.workspaces.get(project_id)
        workspace.media_dir.mkdir(parents=True, exist_ok=True)
        source = workspace.media_dir / "sync.mkv"
        subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x240:rate=30:duration=2",
                "-itsoffset",
                "0.5",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1.5",
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-copyts",
                str(source),
            ],
            check=True,
            capture_output=True,
        )
        diagnosis = services.diagnose_media(project_id, "media/sync.mkv")
        assert diagnosis.probe_ok is True
        assert any(item.code == "av_desync" for item in diagnosis.issues)
        assert diagnosis.recommended == "audio_sync"

        result = services.repair_media(
            project_id,
            RepairRequest(src="media/sync.mkv", strategy="auto"),
        )
        assert result["used_strategy"] == "audio_sync"
        assert Path(result["output_abs"]).is_file()
        after = result["diagnosis_after"]
        assert not any(
            item["code"] == "av_desync" for item in after["issues"]
        )
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_repair_corrupt_file_fails_cleanly(settings: Settings) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("broken-media")["id"]
        workspace = services.workspaces.get(project_id)
        workspace.media_dir.mkdir(parents=True, exist_ok=True)
        (workspace.media_dir / "broken.mp4").write_bytes(b"not a media file")
        diagnosis = services.diagnose_media(project_id, "media/broken.mp4")
        assert diagnosis.probe_ok is False
        assert diagnosis.issues[0].code == "unopenable"
        with pytest.raises(ToolExecutionError):
            services.repair_media(
                project_id,
                RepairRequest(src="media/broken.mp4", strategy="remux"),
            )
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_repair_remux_falls_back_to_reencode(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = _ingested(services, synthetic_video)
        original = services._execute_tool
        calls: list[str] = []

        def flaky(executor, call, *, timeout_seconds=None):
            calls.append(str(call.args.get("strategy")))
            if len(calls) == 1:
                raise ToolExecutionError("remux failed")
            return original(
                executor, call, timeout_seconds=timeout_seconds
            )

        services._execute_tool = flaky  # type: ignore[method-assign]
        result = services.repair_media(
            project_id, RepairRequest(strategy="remux")
        )
        assert calls == ["remux", "reencode"]
        assert result["used_strategy"] == "reencode"
        assert Path(result["output_abs"]).is_file()
        assert any("回退" in item for item in result["warnings"])
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_audio_only_transcode_with_explicit_source(
    settings: Settings,
    ffmpeg_bin: str,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("audio-only")["id"]
        workspace = services.workspaces.get(project_id)
        workspace.audio_dir.mkdir(parents=True, exist_ok=True)
        source = workspace.audio_dir / "tone.wav"
        subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:a",
                "pcm_s16le",
                str(source),
            ],
            check=True,
            capture_output=True,
        )
        request = TranscodeRequest(
            src="audio/tone.wav",
            dst="exports/tone.m4a",
            profile=TranscodeProfile(
                media_type="audio",
                container="m4a",
                audio_tracks=[AudioOutput(codec="aac", bitrate="64k")],
            ),
        )
        first = services.transcode(project_id, request)
        assert first["media_type"] == "audio"
        assert first["audio_codec"] == "aac"
        assert Path(first["output_abs"]).is_file()
        assert first["cached"] is False
        second = services.transcode(project_id, request)
        assert second["cached"] is True
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_interpret_media_goal_honors_explicit_source(
    settings: Settings,
    ffmpeg_bin: str,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("explicit-source")["id"]
        workspace = services.workspaces.get(project_id)
        workspace.audio_dir.mkdir(parents=True, exist_ok=True)
        source = workspace.audio_dir / "tone.wav"
        subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:a",
                "pcm_s16le",
                str(source),
            ],
            check=True,
            capture_output=True,
        )
        plan = services.interpret_media_goal(
            project_id,
            "转成 mp3",
            src="audio/tone.wav",
            use_llm=False,
        )
        assert plan is not None
        assert plan.transcode is not None
        assert plan.transcode.src == "audio/tone.wav"
        assert plan.transcode.profile.media_type == "audio"
        assert plan.transcode.profile.container == "mp3"

        result = services.run_media_operation(
            project_id,
            "转成 mp3",
            src="audio/tone.wav",
        )
        assert result["operation"] == "convert"
        assert Path(result["output_abs"]).is_file()
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_interpret_and_run_compress(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = _ingested(services, synthetic_video)
        plan = services.interpret_media_goal(
            project_id, "转成 mkv", use_llm=False
        )
        assert plan is not None
        assert plan.transcode is not None
        assert plan.transcode.profile.container == "mkv"

        compressed = services.run_media_operation(
            project_id, "压缩到 720p，体积小一点"
        )
        assert compressed["operation"] == "compress"
        assert Path(compressed["output_abs"]).is_file()
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_llm_refines_low_confidence_goal(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    original_planner = services.planner
    try:
        project_id = _ingested(services, synthetic_video)

        class FakeLLM:
            available = True

            def chat_json(self, messages, model, **_kwargs):
                return model(
                    operation="compress",
                    crf=30,
                    confidence=0.9,
                    reason="llm",
                )

        class FakePlanner:
            available = True
            llm_available = True
            llm = FakeLLM()

            def close(self) -> None:
                return None

        services.planner = FakePlanner()  # type: ignore[assignment]
        plan = services.interpret_media_goal(
            project_id, "帮我处理一下这个视频", use_llm=True
        )
        assert plan is not None
        assert plan.operation == "compress"
        assert plan.transcode is not None
        assert plan.transcode.profile.crf == 30
    finally:
        services.planner = original_planner
        services.close()
        jobs.shutdown()
        db.dispose()
