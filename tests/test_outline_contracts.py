from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from smartcut.contracts.chapters import Chapter, OutlineApproval
from smartcut.contracts.time import RationalTime
from smartcut.server.db import Database


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def make_approval() -> OutlineApproval:
    return OutlineApproval(
        project_id="p_test",
        run_id="run_test",
        src="media/a.mp4",
        source_hash="a" * 64,
        goal="cut",
        chapters=[
            Chapter(id="ch_0001", start=_rt("0"), end=_rt("10"), title="Intro")
        ],
    )


def test_outline_approval_validates_unique_chapters() -> None:
    approval = make_approval()
    assert approval.chapters[0].title == "Intro"
    with pytest.raises(ValidationError):
        OutlineApproval(
            project_id="p_test",
            run_id="run_test",
            src="media/a.mp4",
            source_hash="a" * 64,
            chapters=[approval.chapters[0], approval.chapters[0]],
        )


def test_database_migrates_old_orchestration_runs(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE orchestration_runs (
            id VARCHAR(64) PRIMARY KEY,
            project_id VARCHAR(64),
            goal TEXT,
            status VARCHAR(16),
            allow_render BOOLEAN,
            manual_ir_json TEXT,
            plan_json TEXT,
            ledger_json TEXT,
            answer TEXT,
            confidence FLOAT,
            artifacts_json TEXT,
            warnings_json TEXT,
            error TEXT,
            created_at DATETIME,
            updated_at DATETIME
        );
        INSERT INTO orchestration_runs(id, project_id, goal, status)
        VALUES ('run_old', 'p_old', 'old', 'succeeded');
        CREATE TABLE media_assets (
            id VARCHAR(64) PRIMARY KEY,
            project_id VARCHAR(64),
            src TEXT,
            facts_json TEXT,
            transcript_json TEXT,
            created_at DATETIME
        );
        INSERT INTO media_assets(id, project_id, src, facts_json)
        VALUES ('m_old', 'p_old', 'media/a.mp4', '{}');
        """
    )
    connection.commit()
    connection.close()

    db = Database(path)
    db.init()
    try:
        with db.engine.connect() as conn:
            columns = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(orchestration_runs)")
            }
            assert {"phase", "approved_outline_id", "metrics_json"} <= columns
            row = conn.exec_driver_sql(
                "SELECT phase, approved_outline_id FROM orchestration_runs "
                "WHERE id = 'run_old'"
            ).fetchone()
            assert row == ("plan", None)
            tables = {
                item[0]
                for item in conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "outline_approvals" in tables
            assert "project_audio_assets" in tables
            asset_columns = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(media_assets)")
            }
            assert "source_hash" in asset_columns
            assert conn.exec_driver_sql(
                "SELECT source_hash FROM media_assets WHERE id='m_old'"
            ).fetchone() == (None,)
    finally:
        db.dispose()
