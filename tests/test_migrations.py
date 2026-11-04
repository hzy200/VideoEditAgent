from __future__ import annotations

import sqlite3
from pathlib import Path

from smartcut.server.db import SCHEMA_VERSION, Database

GOLDEN_OLD_DB = Path(__file__).parent / "golden" / "old_db_v0.sql"


def _make_old_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(GOLDEN_OLD_DB.read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()


def test_old_db_migrates_to_latest_version(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    _make_old_db(path)

    db = Database(path)
    db.init()
    try:
        assert db.schema_version() == SCHEMA_VERSION
        with db.engine.connect() as conn:
            run_columns = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(orchestration_runs)"
                )
            }
            assert {
                "phase",
                "approved_outline_id",
                "metrics_json",
                "pinned_effects_json",
            } <= run_columns
            assert conn.exec_driver_sql(
                "SELECT phase FROM orchestration_runs WHERE id='run_old'"
            ).fetchone() == ("plan",)

            asset_columns = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(media_assets)")
            }
            assert "source_hash" in asset_columns

            feedback_columns = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(edit_feedback)")
            }
            assert "source_hash" in feedback_columns
            assert conn.exec_driver_sql(
                "SELECT COUNT(*) FROM edit_feedback WHERE project_id='p_old'"
            ).fetchone() == (1,)

            feedback_indexes = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA index_list('edit_feedback')")
            }
            assert "ix_edit_feedback_source_hash" in feedback_indexes
            artifact_indexes = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA index_list('artifacts')")
            }
            assert "ix_artifacts_project_kind_updated" in artifact_indexes
    finally:
        db.dispose()


def test_migration_is_idempotent_on_second_init(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    _make_old_db(path)

    first = Database(path)
    first.init()
    first.dispose()

    second = Database(path)
    second.init()
    try:
        assert second.schema_version() == SCHEMA_VERSION
        with second.engine.connect() as conn:
            assert conn.exec_driver_sql(
                "SELECT COUNT(*) FROM orchestration_runs WHERE id='run_old'"
            ).fetchone() == (1,)
            assert conn.exec_driver_sql(
                "SELECT COUNT(*) FROM media_assets WHERE id='m_old'"
            ).fetchone() == (1,)
            assert conn.exec_driver_sql(
                "SELECT COUNT(*) FROM schema_meta WHERE key='schema_version'"
            ).fetchone() == (1,)
    finally:
        second.dispose()
