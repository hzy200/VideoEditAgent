from __future__ import annotations

from pathlib import Path

import pytest

from smartcut.config import Settings
from smartcut.contracts.media_ops import RepairRequest
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services
from smartcut.server.services.media_tools import MediaToolsMixin


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _facts(
    *,
    video_start: str = "0",
    audio_start: str = "0",
    video_duration: str = "10",
    audio_duration: str = "10",
    vfr: bool = False,
) -> MediaFacts:
    return MediaFacts(
        src="media/source.mp4",
        duration=_rt("10"),
        fps="30",
        vfr=vfr,
        video=StreamFacts(
            codec_type="video",
            codec_name="h264",
            width=320,
            height=240,
            start_time=_rt(video_start),
            duration=_rt(video_duration),
        ),
        audio=StreamFacts(
            codec_type="audio",
            codec_name="aac",
            start_time=_rt(audio_start),
            duration=_rt(audio_duration),
        ),
    )


class _FakeExecutor:
    def __init__(self, facts: MediaFacts | None, *, fail: bool = False) -> None:
        self.facts = facts
        self.fail = fail

    def probe(self, _src: str) -> MediaFacts:
        if self.fail:
            raise RuntimeError("moov atom not found")
        assert self.facts is not None
        return self.facts


def _services(settings: Settings) -> tuple[Services, JobService, Database]:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    return Services(settings, db, jobs, transcriber=NullTranscriber()), jobs, db


def _project(services: Services, source: Path) -> str:
    project_id = services.create_project("diagnose")["id"]
    services.ingest(project_id, str(source))
    return project_id


def test_diagnose_detects_av_desync_and_blur(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = _project(services, synthetic_video)
        facts = _facts(audio_start="0.5", audio_duration="9.4")
        services._executor = lambda *args, **kwargs: _FakeExecutor(facts)  # type: ignore[method-assign]
        services.get_perception = lambda _pid: {  # type: ignore[method-assign]
            "quality": {
                "src": facts.src,
                "frame_count": 10,
                "sampled_fps": 1.0,
                "engine": "thread",
                "marks": [
                    {
                        "t": _rt("1.0").model_dump(),
                        "kind": "blurry",
                        "score": 0.9,
                    }
                ],
            }
        }
        diagnosis = services.diagnose_media(project_id)
        assert diagnosis.probe_ok is True
        codes = {issue.code for issue in diagnosis.issues}
        assert "av_desync" in codes
        assert "blurry" in codes
        assert diagnosis.recommended == "audio_sync"
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_diagnose_vfr_recommends_reencode(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = _project(services, synthetic_video)
        facts = _facts(vfr=True)
        services._executor = lambda *args, **kwargs: _FakeExecutor(facts)  # type: ignore[method-assign]
        services.get_perception = lambda _pid: {}  # type: ignore[method-assign]
        diagnosis = services.diagnose_media(project_id)
        assert any(item.code == "vfr_timestamps" for item in diagnosis.issues)
        assert diagnosis.recommended == "reencode"
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_repair_offset_advances_late_audio() -> None:
    facts = _facts(audio_start="0.5", audio_duration="9.5")
    offset = MediaToolsMixin._repair_offset(
        RepairRequest(strategy="audio_sync"), facts, "audio_sync"
    )
    assert offset == pytest.approx(-0.5)


def test_diagnose_probe_failure_returns_unopenable(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = _project(services, synthetic_video)
        services._executor = lambda *args, **kwargs: _FakeExecutor(None, fail=True)  # type: ignore[method-assign]
        diagnosis = services.diagnose_media(project_id)
        assert diagnosis.probe_ok is False
        assert diagnosis.issues[0].code == "unopenable"
        assert diagnosis.recommended == "remux"
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()
