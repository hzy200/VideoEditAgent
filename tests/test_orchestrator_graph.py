from __future__ import annotations

import threading
import time
from typing import Any, cast

import pytest

from smartcut.cancel import CancellationToken
from smartcut.config import Settings
from smartcut.errors import IRValidationError, RunCancelledError
from smartcut.orchestrator.context import AgentContext, CancellableToolkit
from smartcut.orchestrator.graph import TaskGraphExecutor, validate_graph
from smartcut.orchestrator.models import (
    AgentResult,
    OrchestrationPlan,
    OrchestratorTask,
    TaskStatus,
)
from smartcut.runner.paths import Workspace


def make_context(tmp_path) -> AgentContext:
    settings = Settings(workspace_root=tmp_path / "ws", deepseek_api_key=None)
    workspace = Workspace(tmp_path / "ws")
    return AgentContext(
        run_id="run_test",
        project_id="p_test",
        goal="test",
        toolkit=cast(Any, object()),
        workspace=workspace,
        settings=settings,
    )


class RecordingAgent:
    def __init__(
        self,
        name: str,
        *,
        barrier: threading.Barrier | None = None,
        calls: list[str] | None = None,
        delay: float = 0.0,
        fail_times: int = 0,
        new_tasks=None,
    ) -> None:
        self.name = name
        self.description = name
        self.barrier = barrier
        self.calls = calls if calls is not None else []
        self.delay = delay
        self.fail_times = fail_times
        self.new_tasks = new_tasks or []
        self._lock = threading.Lock()

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(f"{self.name} boom")
        if self.delay:
            time.sleep(self.delay)
        if self.barrier is not None:
            self.barrier.wait(timeout=3)
        with self._lock:
            self.calls.append(task.id)
        if self.new_tasks:
            return AgentResult(summary=task.id, new_tasks=self.new_tasks)
        return AgentResult(summary=task.id)


def test_independent_tasks_run_in_parallel(tmp_path) -> None:
    barrier = threading.Barrier(2)
    calls: list[str] = []
    agents = {
        "a1": RecordingAgent("a1", barrier=barrier, calls=calls),
        "a2": RecordingAgent("a2", barrier=barrier, calls=calls),
        "join": RecordingAgent("join", calls=calls),
    }
    plan = OrchestrationPlan(
        tasks=[
            OrchestratorTask(id="task1", agent="a1"),
            OrchestratorTask(id="task2", agent="a2"),
            OrchestratorTask(id="join", agent="join", depends_on=["task1", "task2"]),
        ]
    )
    executor = TaskGraphExecutor(agents, max_parallel=2, max_rounds=4)
    outcomes, ok = executor.run(plan, make_context(tmp_path))

    assert ok is True
    assert set(calls[:2]) == {"task1", "task2"}
    assert calls[-1] == "join"
    assert all(item.status == TaskStatus.succeeded for item in outcomes)
    assert all(item.queue_wait_ms >= 0 and item.duration_ms >= 0 for item in outcomes)
    assert all(item.retries == 0 for item in outcomes)


def test_agent_can_dynamically_add_task(tmp_path) -> None:
    calls: list[str] = []
    child = OrchestratorTask(id="child", agent="child", depends_on=["parent"])
    agents = {
        "parent": RecordingAgent("parent", calls=calls, new_tasks=[child]),
        "child": RecordingAgent("child", calls=calls),
    }
    plan = OrchestrationPlan(tasks=[OrchestratorTask(id="parent", agent="parent")])
    executor = TaskGraphExecutor(agents, max_parallel=2, max_rounds=4)
    ctx = make_context(tmp_path)
    outcomes, ok = executor.run(plan, ctx)

    assert ok is True
    assert [item.task_id for item in outcomes] == ["parent", "child"]
    assert calls == ["parent", "child"]
    assert ctx.dynamic_tasks() == ["child"]


def test_dynamic_task_rejects_duplicate_agent(tmp_path) -> None:
    first = OrchestratorTask(id="child1", agent="child", depends_on=["parent"])
    second = OrchestratorTask(id="child2", agent="child", depends_on=["parent"])
    agents = {
        "parent": RecordingAgent(
            "parent",
            new_tasks=[first, second],
        ),
        "child": RecordingAgent("child"),
    }
    plan = OrchestrationPlan(tasks=[OrchestratorTask(id="parent", agent="parent")])
    executor = TaskGraphExecutor(agents, max_parallel=2, max_rounds=4)
    outcomes, ok = executor.run(plan, make_context(tmp_path))

    assert ok is True
    assert "child2" not in {item.task_id for item in outcomes}


def test_dynamic_task_rejects_forbidden_agent(tmp_path) -> None:
    child = OrchestratorTask(id="child", agent="child", depends_on=["parent"])
    agents = {
        "parent": RecordingAgent("parent", new_tasks=[child]),
        "child": RecordingAgent("child"),
    }
    plan = OrchestrationPlan(tasks=[OrchestratorTask(id="parent", agent="parent")])
    executor = TaskGraphExecutor(agents, max_parallel=2, max_rounds=4)
    ctx = make_context(tmp_path)
    ctx.forbidden_agents = frozenset({"child"})
    outcomes, ok = executor.run(plan, ctx)

    assert ok is True
    assert "child" not in {item.task_id for item in outcomes}
    assert ctx.dynamic_tasks() == []


def test_dynamic_task_rejects_agent_not_in_phase_policy(tmp_path) -> None:
    child = OrchestratorTask(id="child", agent="child", depends_on=["parent"])
    agents = {
        "parent": RecordingAgent("parent", new_tasks=[child]),
        "child": RecordingAgent("child"),
    }
    plan = OrchestrationPlan(tasks=[OrchestratorTask(id="parent", agent="parent")])
    executor = TaskGraphExecutor(agents, max_parallel=2, max_rounds=4)
    ctx = make_context(tmp_path)
    ctx.allowed_dynamic_agents = frozenset({"parent"})
    outcomes, ok = executor.run(plan, ctx)

    assert ok is True
    assert "child" not in {item.task_id for item in outcomes}
    assert ctx.dynamic_tasks() == []


def test_cancelled_agent_cannot_write_through_toolkit(tmp_path) -> None:
    class Toolkit:
        def __init__(self) -> None:
            self.writes = 0

        def write(self) -> None:
            self.writes += 1

    class LateWriter:
        name = "late"
        description = "late"

        def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
            while not ctx.cancel_token.cancelled:
                time.sleep(0.01)
            time.sleep(0.05)
            try:
                ctx.toolkit.write()
            except RunCancelledError:
                pass
            return AgentResult(summary="late")

    token = CancellationToken()
    inner = Toolkit()
    ctx = make_context(tmp_path)
    ctx.cancel_token = token
    ctx.toolkit = CancellableToolkit(cast(Any, inner), token)
    executor = TaskGraphExecutor(
        {"late": LateWriter()},
        max_parallel=1,
        max_rounds=4,
    )
    timer = threading.Timer(0.05, token.cancel)
    timer.start()
    try:
        executor.run(
            OrchestrationPlan(tasks=[OrchestratorTask(id="late", agent="late")]),
            ctx,
        )
        time.sleep(0.3)
    finally:
        timer.cancel()
    assert inner.writes == 0


def test_cancel_does_not_wait_for_unresponsive_agent(tmp_path) -> None:
    started = time.monotonic()

    class SlowAgent:
        name = "slow"
        description = "slow"

        def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
            time.sleep(2.0)
            return AgentResult(summary="late")

    token = CancellationToken()
    ctx = make_context(tmp_path)
    ctx.cancel_token = token
    plan = OrchestrationPlan(tasks=[OrchestratorTask(id="slow", agent="slow")])
    executor = TaskGraphExecutor({"slow": SlowAgent()}, max_parallel=1, max_rounds=4)
    threading.Timer(0.1, token.cancel).start()

    outcomes, ok = executor.run(plan, ctx)
    elapsed = time.monotonic() - started

    assert ok is False
    assert elapsed < 1.0
    assert outcomes[0].status == TaskStatus.cancelled


class TimedAgent:
    def __init__(self, name: str, delay: float) -> None:
        self.name = name
        self.description = name
        self.delay = delay
        self.started_at: float | None = None

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        self.started_at = time.monotonic()
        if self.delay:
            time.sleep(self.delay)
        return AgentResult(summary=task.id)


def test_short_branch_does_not_wait_for_long_parallel_task(tmp_path) -> None:
    slow = TimedAgent("slow", 0.5)
    fast = TimedAgent("fast", 0.01)
    child = TimedAgent("child", 0.0)
    agents = {"slow": slow, "fast": fast, "child": child}
    plan = OrchestrationPlan(
        tasks=[
            OrchestratorTask(id="slow", agent="slow"),
            OrchestratorTask(id="fast", agent="fast"),
            OrchestratorTask(id="child", agent="child", depends_on=["fast"]),
        ]
    )
    started = time.monotonic()
    executor = TaskGraphExecutor(agents, max_parallel=2, max_rounds=10)
    outcomes, ok = executor.run(plan, make_context(tmp_path))

    assert ok is True
    assert all(item.status == TaskStatus.succeeded for item in outcomes)
    assert child.started_at is not None
    assert child.started_at - started < 0.3


def test_max_rounds_consumes_completed_results_before_stopping(tmp_path) -> None:
    agents = {"a": RecordingAgent("a")}
    plan = OrchestrationPlan(
        tasks=[OrchestratorTask(id="a", agent="a")]
    )
    executor = TaskGraphExecutor(agents, max_parallel=1, max_rounds=0)
    outcomes, ok = executor.run(plan, make_context(tmp_path))

    assert ok is True
    assert outcomes[0].status == TaskStatus.succeeded


def test_cycle_is_rejected(tmp_path) -> None:
    tasks = [
        OrchestratorTask(id="a", agent="a", depends_on=["b"]),
        OrchestratorTask(id="b", agent="b", depends_on=["a"]),
    ]
    with pytest.raises(IRValidationError):
        validate_graph(tasks, {"a", "b"})


def test_failure_skips_dependents_but_allow_failed_deps_runs(tmp_path) -> None:
    calls: list[str] = []
    agents = {
        "fail": RecordingAgent("fail", calls=calls, fail_times=1),
        "child": RecordingAgent("child", calls=calls),
        "optional": RecordingAgent("optional", calls=calls),
    }
    plan = OrchestrationPlan(
        tasks=[
            OrchestratorTask(id="fail", agent="fail"),
            OrchestratorTask(id="child", agent="child", depends_on=["fail"]),
            OrchestratorTask(
                id="optional",
                agent="optional",
                depends_on=["fail"],
                allow_failed_deps=True,
            ),
        ]
    )
    executor = TaskGraphExecutor(agents, max_parallel=2, max_rounds=4)
    outcomes, ok = executor.run(plan, make_context(tmp_path))
    by_id = {item.task_id: item for item in outcomes}

    assert ok is False
    assert by_id["fail"].status == TaskStatus.failed
    assert by_id["child"].status == TaskStatus.skipped
    assert by_id["optional"].status == TaskStatus.succeeded


def test_failed_agent_is_retried(tmp_path) -> None:
    calls: list[str] = []
    agents = {"flaky": RecordingAgent("flaky", calls=calls, fail_times=1)}
    plan = OrchestrationPlan(
        tasks=[OrchestratorTask(id="flaky", agent="flaky", max_retries=1)]
    )
    executor = TaskGraphExecutor(agents, max_parallel=1, max_rounds=4)
    outcomes, ok = executor.run(plan, make_context(tmp_path))

    assert ok is True
    assert outcomes[0].status == TaskStatus.succeeded
    assert outcomes[0].attempts == 2
    assert outcomes[0].retries == 1


def test_unknown_agent_is_rejected(tmp_path) -> None:
    plan = OrchestrationPlan(tasks=[OrchestratorTask(id="x", agent="nope")])
    executor = TaskGraphExecutor({}, max_parallel=1, max_rounds=1)
    with pytest.raises(IRValidationError):
        executor.run(plan, make_context(tmp_path))
