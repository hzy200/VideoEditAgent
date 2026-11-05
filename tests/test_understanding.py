from __future__ import annotations

from pathlib import Path

from smartcut.config import Settings
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _services(settings: Settings) -> tuple[Services, JobService, Database]:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    return Services(settings, db, jobs, transcriber=NullTranscriber()), jobs, db


def test_understand_video_returns_text_without_revision(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("understand")["id"]
        services.ingest(project_id, str(synthetic_video))
        result = services.understand_video(
            project_id, "这个视频大致讲了什么？"
        )
        assert result["mode"] == "understanding"
        assert "媒体事实" in result["answer"]
        assert "视频内容概览" in result["answer"] or "问题：" in result["answer"]
        assert result["llm_used"] is False
        assert services.list_revisions(project_id) == []
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_understand_video_uses_llm_when_available(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    original = services.planner
    try:
        project_id = services.create_project("understand-llm")["id"]
        services.ingest(project_id, str(synthetic_video))

        class FakeLLM:
            available = True

            def chat_text(self, messages, **_kwargs):
                return "这是基于视频内容的回答。"

        class FakePlanner:
            available = True
            llm_available = True
            llm = FakeLLM()

            def close(self) -> None:
                return None

        services.planner = FakePlanner()  # type: ignore[assignment]
        result = services.understand_video(project_id, "总结一下")
        assert result["llm_used"] is True
        assert result["answer"] == "这是基于视频内容的回答。"
        assert services.list_revisions(project_id) == []
    finally:
        services.planner = original
        services.close()
        jobs.shutdown()
        db.dispose()
