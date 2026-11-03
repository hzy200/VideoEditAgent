from __future__ import annotations

import subprocess
from pathlib import Path

from smartcut.config import Settings
from smartcut.contracts.edit_ir import AudioSpec, EditIR, TimeRange, TrimOp
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.time import RationalTime
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _wav(path: Path, ffmpeg_bin: str) -> Path:
    subprocess.run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _services(settings: Settings) -> tuple[Services, JobService, Database]:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    return Services(settings, db, jobs, transcriber=NullTranscriber()), jobs, db


def test_audio_import_dedupes_same_hash_and_name(
    settings: Settings, tmp_path: Path, ffmpeg_bin: str
) -> None:
    wav = _wav(tmp_path / "bgm.wav", ffmpeg_bin)
    services, jobs, db = _services(settings)
    try:
        pid = services.create_project("dedupe")["id"]
        first = services.import_audio_asset(pid, str(wav), "BGM")
        second = services.import_audio_asset(pid, str(wav), "BGM")
        assert first["id"] == second["id"]
        assert len(services.list_audio_assets(pid)) == 1
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_audio_waveforms_include_main_and_assets(
    settings: Settings, tmp_path: Path, ffmpeg_bin: str, synthetic_video: Path
) -> None:
    wav = _wav(tmp_path / "bgm.wav", ffmpeg_bin)
    services, jobs, db = _services(settings)
    try:
        pid = services.create_project("wave")["id"]
        services.ingest(pid, str(synthetic_video))
        asset = services.import_audio_asset(pid, str(wav), "BGM")
        data = services.build_audio_waveforms(pid, buckets=64)
        assert data["main"]
        assert data["assets"][asset["id"]]
        again = services.build_audio_waveforms(pid, buckets=64)
        assert again["main"] == data["main"]
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_preview_mix_legacy_global_volume(
    settings: Settings, synthetic_video: Path
) -> None:
    services, jobs, db = _services(settings)
    try:
        pid = services.create_project("preview")["id"]
        asset = services.ingest(pid, str(synthetic_video))
        facts = services.get_project(pid)["asset"]["facts"]
        ir = EditIR(
            project_id=pid,
            revision=1,
            media_facts=MediaFacts.model_validate(facts),
            sequence=[
                TrimOp(
                    src=asset["src"],
                    range=TimeRange(start=_rt("0"), end=_rt("1")),
                )
            ],
            audio=AudioSpec(volume_db=-3.0),
        )
        result = services.build_preview_mix(pid, ir)
        assert Path(result["path"]).exists()
        assert result["legacy"] is False
        again = services.build_preview_mix(pid, ir)
        assert again["cached"] is True
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_diagnostics_includes_demucs_and_encoder(
    settings: Settings,
) -> None:
    services, jobs, db = _services(settings)
    try:
        info = services.diagnostics()["info"]
        for key in (
            "demucs_available",
            "demucs_model",
            "encoder_priority",
            "vision_configured",
        ):
            assert key in info
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_revision_roundtrip_preserves_audio_tracks(
    settings: Settings, synthetic_video: Path
) -> None:
    services, jobs, db = _services(settings)
    try:
        pid = services.create_project("tracks")["id"]
        asset = services.ingest(pid, str(synthetic_video))
        facts = MediaFacts.model_validate(
            services.get_project(pid)["asset"]["facts"]
        )
        ir = EditIR(
            project_id=pid,
            media_facts=facts,
            sequence=[
                TrimOp(
                    src=asset["src"],
                    range=TimeRange(start=_rt("0"), end=_rt("1")),
                )
            ],
            audio=AudioSpec(
                tracks=[
                    {
                        "id": "main",
                        "role": "main",
                        "asset_id": "main",
                        "source_end": _rt("1"),
                        "loop": True,
                    }
                ]
            ),
        )
        submitted = services.submit_revision(pid, ir)
        loaded = services.get_revision(pid, submitted["revision"])
        assert loaded.audio.tracks is not None
        assert loaded.audio.tracks[0].loop is True
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
