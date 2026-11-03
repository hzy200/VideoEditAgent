from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from smartcut.config import Settings
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database, OrchestrationRun
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _services(settings: Settings) -> tuple[Services, JobService, Database]:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    return Services(settings, db, jobs, transcriber=NullTranscriber()), jobs, db


def test_recent_run_digests_extracts_qa_and_structural_changes(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("history")["id"]
        services.ingest(project_id, str(synthetic_video))
        ledger = [
            {"agent": "plan", "status": "succeeded", "output": {"revision": 3}},
            {
                "agent": "qa",
                "status": "succeeded",
                "output": {"score": 0.42, "summary": "节奏偏慢", "issues": []},
            },
            {
                "agent": "critic",
                "status": "succeeded",
                "output": {"changed": True, "changes": ["拆分长片段"]},
            },
            {
                "agent": "audio",
                "status": "succeeded",
                "output": {"changed": True, "removed_words": 4},
            },
        ]
        with db.session() as session:
            session.add(
                OrchestrationRun(
                    id="run_hist01",
                    project_id=project_id,
                    goal="第一版目标",
                    status="succeeded",
                    phase="plan",
                    ledger_json=json.dumps(ledger, ensure_ascii=False),
                    created_at=datetime.now(),
                )
            )
            session.add(
                OrchestrationRun(
                    id="run_hist02",
                    project_id=project_id,
                    goal="仍在运行",
                    status="running",
                    phase="plan",
                    created_at=datetime.now(),
                )
            )

        digests = services.recent_run_digests(project_id)

        assert len(digests) == 1
        digest = digests[0]
        assert digest["run_id"] == "run_hist01"
        assert digest["goal"] == "第一版目标"
        assert digest["revision"] == 3
        assert digest["qa_score"] == 0.42
        assert digest["qa_summary"] == "节奏偏慢"
        assert len(digest["changes"]) == 2
        assert services.recent_run_digests(
            project_id, exclude_run_id="run_hist01"
        ) == []
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()
