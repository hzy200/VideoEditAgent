"""SQLite 持久化：项目、素材、IR 版本、任务、工具调用审计。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

SCHEMA_VERSION = 5


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    workspace_dir: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    src: Mapped[str] = mapped_column(Text)
    facts_json: Mapped[str] = mapped_column(Text)
    transcript_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProjectAudioAsset(Base):
    """项目级音频资产：BGM、配音、分离 stem 等。"""

    __tablename__ = "project_audio_assets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    src: Mapped[str] = mapped_column(Text)
    facts_json: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProjectOverlayAsset(Base):
    """项目级图片/Logo 水印资产。"""

    __tablename__ = "project_overlay_assets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    src: Mapped[str] = mapped_column(Text)
    mime: Mapped[str | None] = mapped_column(String(64), nullable=True)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Revision(Base):
    __tablename__ = "revisions"
    __table_args__ = (UniqueConstraint("project_id", "revision_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    ir_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Artifact(Base):
    """内容寻址缓存索引：cache_key -> 工作区文件/目录。"""

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("project_id", "kind", "cache_key"),
        Index(
            "ix_artifacts_project_kind_updated",
            "project_id",
            "kind",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    source_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    path: Mapped[str] = mapped_column(Text)
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class OutlineApprovalRecord(Base):
    """人工确认的大纲快照；Phase B 通过 id 引用。"""

    __tablename__ = "outline_approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    source_hash: Mapped[str] = mapped_column(String(64), index=True)
    goal: Mapped[str] = mapped_column(Text)
    chapters_json: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OrchestrationRun(Base):
    """Orchestrator 运行记录：任务图、账本、最终答案与产物。"""

    __tablename__ = "orchestration_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    allow_render: Mapped[bool] = mapped_column(Boolean, default=True)
    phase: Mapped[str] = mapped_column(String(16), default="plan")
    approved_outline_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manual_ir_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    pinned_effects_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ledger_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    artifacts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class OrchestrationTaskOutcome(Base):
    """运行账本的增量检查点：每个 task 一行，按 run/task 幂等 upsert。"""

    __tablename__ = "orchestration_task_outcomes"
    __table_args__ = (UniqueConstraint("run_id", "task_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    data_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class EditFeedback(Base):
    """用户编辑反馈：只存结构化统计，不存媒体内容。"""

    __tablename__ = "edit_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    # 按素材哈希隔离：换素材后旧节奏统计不再参与规划。
    source_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ToolCallRecord(Base):
    __tablename__ = "tool_call_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    tool: Mapped[str] = mapped_column(String(32))
    argv_json: Mapped[str] = mapped_column(Text)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifacts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def _table_columns(conn: Connection, table: str) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def _add_column(conn: Connection, table: str, column: str, ddl: str) -> None:
    if column in _table_columns(conn, table):
        return
    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migration_v1(conn: Connection) -> None:
    _add_column(
        conn,
        "orchestration_runs",
        "phase",
        "phase VARCHAR(16) NOT NULL DEFAULT 'plan'",
    )
    _add_column(
        conn,
        "orchestration_runs",
        "approved_outline_id",
        "approved_outline_id VARCHAR(64)",
    )


def _migration_v2(conn: Connection) -> None:
    _add_column(conn, "orchestration_runs", "metrics_json", "metrics_json TEXT")
    _add_column(
        conn,
        "orchestration_runs",
        "pinned_effects_json",
        "pinned_effects_json TEXT",
    )


def _migration_v3(conn: Connection) -> None:
    _add_column(conn, "media_assets", "source_hash", "source_hash VARCHAR(64)")


def _migration_v4(conn: Connection) -> None:
    _add_column(conn, "edit_feedback", "source_hash", "source_hash VARCHAR(64)")


def _migration_v5(conn: Connection) -> None:
    _add_column(
        conn,
        "media_assets",
        "is_primary",
        "is_primary BOOLEAN NOT NULL DEFAULT 0",
    )
    # 旧库每个项目保留最近一条素材为主素材。
    rows = conn.exec_driver_sql(
        "SELECT project_id, id FROM media_assets "
        "ORDER BY project_id, created_at DESC, id DESC"
    ).fetchall()
    seen: set[str] = set()
    for project_id, asset_id in rows:
        if project_id in seen:
            continue
        seen.add(project_id)
        conn.exec_driver_sql(
            "UPDATE media_assets SET is_primary = 1 WHERE id = ?",
            (asset_id,),
        )


def _ensure_indexes(conn: Connection) -> None:
    # create_all 不会为已存在的旧表补索引；每次 init 幂等补齐。
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_edit_feedback_source_hash "
        "ON edit_feedback (source_hash)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_artifacts_project_kind_updated "
        "ON artifacts (project_id, kind, updated_at)"
    )


MIGRATIONS: tuple[tuple[int, Callable[[Connection], None]], ...] = (
    (1, _migration_v1),
    (2, _migration_v2),
    (3, _migration_v3),
    (4, _migration_v4),
    (5, _migration_v5),
)


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{path.as_posix()}",
            connect_args={"check_same_thread": False},
            future=True,
        )

        @event.listens_for(self.engine, "connect")
        def _pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def init(self) -> None:
        Base.metadata.create_all(self.engine)
        self._migrate()

    def _migrate(self) -> None:
        """版本化增量迁移：旧库按 v1→v4 补列，新表由 create_all 创建。

        版本记录在 ``schema_meta``；每一步与版本更新在同一个事务内提交，
        迁移中途失败时不会留下“版本已升级但列未补齐”的半成品。
        """
        with self.engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS schema_meta ("
                "key VARCHAR(64) PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = conn.exec_driver_sql(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            current = int(row[0]) if row is not None else 0
            for version, step in MIGRATIONS:
                if version <= current:
                    continue
                step(conn)
                conn.exec_driver_sql(
                    "DELETE FROM schema_meta WHERE key = 'schema_version'"
                )
                conn.exec_driver_sql(
                    "INSERT INTO schema_meta(key, value) "
                    "VALUES('schema_version', ?)",
                    (str(version),),
                )
            # 索引修复每次 init 都幂等执行：create_all 不会给旧表补索引，
            # 已升级到最新版本的旧库也可能缺少后来新增的索引。
            _ensure_indexes(conn)

    def schema_version(self) -> int:
        with self.engine.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            return int(row[0]) if row is not None else 0

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
