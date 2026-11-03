from __future__ import annotations

import json
from typing import Any, cast

import pytest
from sqlalchemy import func, select

from smartcut.config import Settings
from smartcut.errors import IRValidationError
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.graph import TaskGraphExecutor
from smartcut.orchestrator.models import (
    AgentResult,
    OrchestrationPlan,
    OrchestrationResult,
    OrchestratorTask,
    TaskOutcome,
    TaskStatus,
)
from smartcut.perception.transcribe import NullTranscriber
from smartcut.runner.paths import Workspace
from smartcut.server.db import (
    Database,
    Job,
    OrchestrationRun,
    OrchestrationTaskOutcome,
)
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


class _Agent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        return AgentResult(summary=task.id)


def _ctx(tmp_path) -> AgentContext:
    return AgentContext(
        run_id="run_delta",
        project_id="p_delta",
        goal="delta",
        toolkit=cast(Any, object()),
        workspace=Workspace(tmp_path / "ws"),
        settings=Settings(workspace_root=tmp_path / "ws"),
    )


def test_graph_callback_receives_only_new_outcomes(tmp_path) -> None:
    batches: list[list[str]] = []
    agents = {name: _Agent(name) for name in ("a", "b", "c")}
    plan = OrchestrationPlan(
        tasks=[
            OrchestratorTask(id="a", agent="a"),
            OrchestratorTask(id="b", agent="b", depends_on=["a"]),
            OrchestratorTask(id="c", agent="c", depends_on=["b"]),
        ]
    )
    outcomes, ok = TaskGraphExecutor(agents, max_parallel=2, max_rounds=8).run(
        plan,
        _ctx(tmp_path),
        lambda items: batches.append([item.task_id for item in items]),
    )
    flat = [task_id for batch in batches for task_id in batch]
    assert ok is True
    assert sorted(flat) == ["a", "b", "c"]
    assert len(flat) == len(set(flat))
    assert all(batch for batch in batches)
    assert len(outcomes) == 3


def _services(settings: Settings) -> tuple[Services, JobService, Database]:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    return Services(settings, db, jobs, transcriber=NullTranscriber()), jobs, db


def _outcome_count(db: Database, run_id: str) -> int:
    with db.session() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(OrchestrationTaskOutcome)
                .where(OrchestrationTaskOutcome.run_id == run_id)
            )
            or 0
        )


def test_orchestration_checkpoint_upserts_task_rows(
    settings: Settings,
    synthetic_video,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("ledger")["id"]
        services.ingest(project_id, str(synthetic_video))
        run = services.create_orchestration_run(
            project_id,
            "分析",
            allow_render=False,
        )
        result = services.orchestrate_run(run["id"])
        assert result["status"] in {"succeeded", "partial"}, result["answer"]

        with db.session() as session:
            rows = session.scalars(
                select(OrchestrationTaskOutcome).where(
                    OrchestrationTaskOutcome.run_id == run["id"]
                )
            ).all()
            assert len(rows) == len(result["ledger"])
            assert all(json.loads(row.data_json)["task_id"] == row.task_id for row in rows)
            # 模拟进程在最终 full snapshot 之前中断：增量表仍可恢复账本。
            run_row = session.get(OrchestrationRun, run["id"])
            run_row.ledger_json = None
            row_count = len(rows)

        recovered = services.get_orchestration_run(run["id"])
        assert len(recovered["ledger"]) == row_count
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_checkpoint_upsert_updates_existing_row(
    settings: Settings,
    synthetic_video,
    monkeypatch,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("upsert")["id"]
        services.ingest(project_id, str(synthetic_video))
        run = services.create_orchestration_run(
            project_id,
            "分析",
            allow_render=False,
        )
        failed = TaskOutcome(
            task_id="task_a",
            agent="analyze",
            status=TaskStatus.failed,
            summary="first",
        )
        succeeded = TaskOutcome(
            task_id="task_a",
            agent="analyze",
            status=TaskStatus.succeeded,
            summary="second",
        )
        plan = OrchestrationPlan(
            tasks=[OrchestratorTask(id="task_a", agent="analyze")]
        )

        def fake_run(**kwargs) -> OrchestrationResult:
            kwargs["on_task_complete"]([failed])
            kwargs["on_task_complete"]([succeeded])
            return OrchestrationResult(
                run_id=kwargs["run_id"],
                project_id=kwargs["project_id"],
                goal=kwargs["goal"],
                status="succeeded",
                answer="ok",
                confidence=1.0,
                plan=plan,
                ledger=[succeeded],
            )

        monkeypatch.setattr(services.orchestrator, "run", fake_run)
        services.orchestrate_run(run["id"])

        with db.session() as session:
            rows = session.scalars(
                select(OrchestrationTaskOutcome).where(
                    OrchestrationTaskOutcome.run_id == run["id"]
                )
            ).all()
        assert len(rows) == 1
        assert rows[0].status == "succeeded"
        assert json.loads(rows[0].data_json)["summary"] == "second"
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_delete_project_cascades_task_outcomes(
    settings: Settings,
    synthetic_video,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("cascade")["id"]
        services.ingest(project_id, str(synthetic_video))
        run = services.create_orchestration_run(
            project_id,
            "分析",
            allow_render=False,
        )
        services.orchestrate_run(run["id"])
        assert _outcome_count(db, run["id"]) > 0

        services.delete_project(project_id)
        assert _outcome_count(db, run["id"]) == 0
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()


def test_delete_project_rejects_active_job_and_keeps_outcomes(
    settings: Settings,
    synthetic_video,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("active")["id"]
        services.ingest(project_id, str(synthetic_video))
        run = services.create_orchestration_run(
            project_id,
            "分析",
            allow_render=False,
        )
        services.orchestrate_run(run["id"])
        before = _outcome_count(db, run["id"])
        assert before > 0

        with db.session() as session:
            session.add(
                Job(
                    id="job_active",
                    project_id=project_id,
                    kind="render",
                    status="running",
                )
            )

        with pytest.raises(IRValidationError):
            services.delete_project(project_id)

        with db.session() as session:
            assert session.get(Job, "job_active") is not None
        assert _outcome_count(db, run["id"]) == before
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()
