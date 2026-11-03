"""动态任务图执行器。

特性：
- 依赖满足的节点并行执行；
- 子智能体可返回 ``new_tasks`` 动态扩展图；
- 失败节点按 ``max_retries`` 重试，失败后依赖节点标记 skipped；
- ``allow_failed_deps`` 允许汇总/决策类任务在部分失败后继续。
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from smartcut.errors import IRValidationError, RunCancelledError
from smartcut.logging import get_logger
from smartcut.orchestrator.base import SubAgent
from smartcut.orchestrator.context import AgentContext, summarize_context
from smartcut.orchestrator.models import (
    AgentResult,
    OrchestrationPlan,
    OrchestratorTask,
    TaskOutcome,
    TaskStatus,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _run_agent(agent: SubAgent, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
    """在工作线程内标记当前 agent，保证共享槽位能记录来源。"""
    ctx.begin_task(task)
    try:
        return agent.run(task, ctx)
    finally:
        ctx.end_task()


def validate_graph(tasks: list[OrchestratorTask], known_agents: set[str]) -> None:
    ids: set[str] = set()
    for task in tasks:
        if task.id in ids:
            raise IRValidationError(f"任务 id 重复: {task.id!r}")
        ids.add(task.id)
        if task.agent not in known_agents:
            raise IRValidationError(f"未知子智能体: {task.agent!r}")

    by_id = {task.id: task for task in tasks}
    for task in tasks:
        for dependency in task.depends_on:
            if dependency not in by_id:
                raise IRValidationError(
                    f"任务 {task.id!r} 依赖不存在的节点 {dependency!r}"
                )

    colors: dict[str, int] = {}

    def visit(node_id: str) -> None:
        state = colors.get(node_id, 0)
        if state == 1:
            raise IRValidationError(f"任务图存在环，节点: {node_id!r}")
        if state == 2:
            return
        colors[node_id] = 1
        for dependency in by_id[node_id].depends_on:
            visit(dependency)
        colors[node_id] = 2

    for task in tasks:
        visit(task.id)


@dataclass
class _Node:
    spec: OrchestratorTask
    status: TaskStatus = TaskStatus.pending
    attempts: int = 0
    enqueued_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: AgentResult | None = None
    context_digest: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_outcome(self) -> TaskOutcome:
        queue_wait_ms = 0
        if self.started_at is not None:
            queue_wait_ms = max(
                0,
                int((self.started_at - self.enqueued_at).total_seconds() * 1000),
            )
        duration_ms = 0
        if self.started_at is not None and self.finished_at is not None:
            duration_ms = max(
                0,
                int((self.finished_at - self.started_at).total_seconds() * 1000),
            )
        attempts = max(self.attempts, 1)
        return TaskOutcome(
            task_id=self.spec.id,
            agent=self.spec.agent,
            description=self.spec.description,
            status=self.status,
            attempts=attempts,
            queue_wait_ms=queue_wait_ms,
            duration_ms=duration_ms,
            retries=max(attempts - 1, 0),
            started_at=self.started_at,
            finished_at=self.finished_at,
            summary=self.result.summary if self.result else "",
            output=self.result.output if self.result else {},
            artifacts=self.result.artifacts if self.result else [],
            warnings=self.result.warnings if self.result else [],
            context_digest=self.context_digest,
            error=self.error,
        )


class TaskGraphExecutor:
    def __init__(
        self,
        agents: dict[str, SubAgent],
        *,
        max_parallel: int = 4,
        max_rounds: int = 64,
        logger: Any | None = None,
    ) -> None:
        self.agents = agents
        self.max_parallel = max(1, max_parallel)
        self.max_rounds = max(1, max_rounds)
        self.logger = logger or get_logger("smartcut.orchestrator.graph")

    def run(
        self,
        plan: OrchestrationPlan,
        ctx: AgentContext,
        on_task_complete=None,
    ) -> tuple[list[TaskOutcome], bool]:
        validate_graph(plan.tasks, set(self.agents))
        nodes: dict[str, _Node] = {task.id: _Node(spec=task) for task in plan.tasks}
        for task in plan.tasks:
            ctx.mark_agent(task.agent)

        reported: set[str] = set()
        terminal = {
            TaskStatus.succeeded,
            TaskStatus.failed,
            TaskStatus.skipped,
            TaskStatus.cancelled,
        }

        def notify() -> None:
            """只回调尚未上报的终态节点，避免每轮全量重写 ledger。"""
            if on_task_complete is None:
                return
            delta: list[TaskOutcome] = []
            for node in nodes.values():
                if node.status in terminal and node.spec.id not in reported:
                    reported.add(node.spec.id)
                    delta.append(node.to_outcome())
            if delta:
                on_task_complete(delta)

        token = ctx.cancel_token
        if token is not None and token.cancelled:
            self._cancel_pending(nodes)
            notify()
            return [node.to_outcome() for node in nodes.values()], False

        rounds = 0
        running: dict[Future, _Node] = {}
        cancelled = False
        safety_stop = False
        pool = ThreadPoolExecutor(
            max_workers=self.max_parallel,
            thread_name_prefix=f"agent-{ctx.run_id[:8]}",
        )
        try:
            while True:
                if token is not None and token.cancelled:
                    self._cancel_pending(nodes)
                    cancelled = True
                    break

                self._skip_blocked(nodes)
                notify()
                pending = [
                    node for node in nodes.values() if node.status == TaskStatus.pending
                ]
                if not pending and not running:
                    break

                # 只受并发上限约束；任一节点完成即继续调度，不再等待整批。
                while len(running) < self.max_parallel and pending:
                    ready = next(
                        (node for node in pending if self._is_ready(node, nodes)),
                        None,
                    )
                    if ready is None:
                        break
                    pending.remove(ready)
                    self._submit(ready, ctx, pool, running)

                if not running:
                    for node in pending:
                        node.status = TaskStatus.skipped
                        node.error = "依赖无法满足或任务图死锁"
                        node.finished_at = _now()
                    break

                # 短轮询而不是无限等待，保证取消在长任务运行期间也能及时生效。
                done, _ = wait(
                    running,
                    timeout=0.2,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    continue

                # 先消费已完成结果，再判断轮次上限；否则本批成功结果会被丢弃。
                for future in done:
                    node = running.pop(future)
                    self._consume(future, node, nodes, ctx)
                rounds += 1
                if rounds > self.max_rounds:
                    safety_stop = True
                    for node in pending:
                        node.status = TaskStatus.skipped
                        node.error = f"超过最大调度轮次 {self.max_rounds}"
                        node.finished_at = _now()
                    break
                notify()
        finally:
            if cancelled or safety_stop:
                # 取消时不无限等待不响应取消的 agent；已提交的 future 自行退出，
                # 结果不再写回 ledger，避免阻塞用户操作。
                for node in nodes.values():
                    if node.status == TaskStatus.running:
                        node.status = TaskStatus.cancelled
                        node.error = node.error or "任务已取消"
                        node.finished_at = node.finished_at or _now()
                pool.shutdown(wait=False, cancel_futures=True)
            else:
                pool.shutdown(wait=True)

        # 安全阀触发时仍未完成的 future 不再等待其结果语义。
        for node in nodes.values():
            if node.status == TaskStatus.running:
                node.status = TaskStatus.cancelled
                node.error = node.error or "调度结束前任务未完成"
                node.finished_at = node.finished_at or _now()

        notify()
        outcomes = [node.to_outcome() for node in nodes.values()]
        all_ok = bool(outcomes) and all(
            outcome.status == TaskStatus.succeeded for outcome in outcomes
        )
        self.logger.info(
            "orchestration_graph_done",
            run_id=ctx.run_id,
            rounds=rounds,
            tasks=len(outcomes),
            all_ok=all_ok,
        )
        return outcomes, all_ok

    @staticmethod
    def _cancel_pending(nodes: dict[str, _Node]) -> None:
        for node in nodes.values():
            if node.status in (TaskStatus.pending, TaskStatus.running):
                node.status = TaskStatus.cancelled
                node.error = "任务已取消"
                node.finished_at = _now()

    def _skip_blocked(self, nodes: dict[str, _Node]) -> None:
        for node in nodes.values():
            if node.status != TaskStatus.pending:
                continue
            for dependency in node.spec.depends_on:
                dep = nodes[dependency]
                if dep.status in (
                    TaskStatus.failed,
                    TaskStatus.skipped,
                    TaskStatus.cancelled,
                ):
                    if node.spec.allow_failed_deps:
                        continue
                    node.status = TaskStatus.skipped
                    node.error = f"依赖 {dependency!r} 未成功，跳过"
                    node.finished_at = _now()
                    break

    @staticmethod
    def _is_ready(node: _Node, nodes: dict[str, _Node]) -> bool:
        for dependency in node.spec.depends_on:
            dep = nodes[dependency]
            if dep.status in (TaskStatus.pending, TaskStatus.running):
                return False
            if (
                dep.status
                in (TaskStatus.failed, TaskStatus.skipped, TaskStatus.cancelled)
                and not node.spec.allow_failed_deps
            ):
                return False
        return True

    def _submit(
        self,
        node: _Node,
        ctx: AgentContext,
        pool: ThreadPoolExecutor,
        running: dict[Future, _Node],
    ) -> None:
        agent = self.agents[node.spec.agent]
        node.status = TaskStatus.running
        node.attempts += 1
        node.started_at = node.started_at or _now()
        node.error = None
        # 在主调度线程抓取快照，保证记录的是该任务真正开始时的输入。
        node.context_digest = summarize_context(ctx, node.spec)
        self.logger.info(
            "agent_start",
            run_id=ctx.run_id,
            task_id=node.spec.id,
            agent=node.spec.agent,
            attempt=node.attempts,
        )
        running[pool.submit(_run_agent, agent, node.spec, ctx)] = node

    def _consume(
        self,
        future: Future,
        node: _Node,
        nodes: dict[str, _Node],
        ctx: AgentContext,
    ) -> None:
        try:
            result = future.result()
        except RunCancelledError as exc:
            node.status = TaskStatus.cancelled
            node.error = f"任务已取消: {exc}"
            node.finished_at = _now()
            self.logger.warning(
                "agent_cancelled",
                run_id=ctx.run_id,
                task_id=node.spec.id,
                agent=node.spec.agent,
            )
            return
        except Exception as exc:  # 智能体错误不能拖垮整张图
            node.error = f"{type(exc).__name__}: {exc}"
            node.finished_at = _now()
            if node.attempts <= node.spec.max_retries:
                node.status = TaskStatus.pending
                node.error = f"第 {node.attempts} 次失败，准备重试: {node.error}"
                self.logger.warning(
                    "agent_retry",
                    run_id=ctx.run_id,
                    task_id=node.spec.id,
                    agent=node.spec.agent,
                    attempt=node.attempts,
                    error=str(exc),
                )
            else:
                node.status = TaskStatus.failed
                self.logger.error(
                    "agent_failed",
                    run_id=ctx.run_id,
                    task_id=node.spec.id,
                    agent=node.spec.agent,
                    error=str(exc),
                )
            return

        # 双保险：即使 agent 内部把取消异常降级成了 warning，也不把该节点
        # 标记为成功，避免取消后继续调度后续节点。
        token = ctx.cancel_token
        if token is not None and token.cancelled:
            node.status = TaskStatus.cancelled
            node.error = "任务已取消"
            node.finished_at = _now()
            self.logger.warning(
                "agent_cancelled_after_return",
                run_id=ctx.run_id,
                task_id=node.spec.id,
                agent=node.spec.agent,
            )
            return

        node.status = TaskStatus.succeeded
        node.result = result
        node.finished_at = _now()
        node.error = None
        try:
            for warning in result.warnings:
                ctx.add_warning(f"{node.spec.id}: {warning}")
            for new_task in result.new_tasks:
                self._add_task(new_task, nodes, ctx)
        except RunCancelledError:
            # ctx.add_warning 会再次检查取消；不要让它逃出 _consume
            # 触发调度器 finally 的 shutdown(wait=True)。
            node.status = TaskStatus.cancelled
            node.error = "任务已取消"
            node.finished_at = _now()
            return
        self.logger.info(
            "agent_done",
            run_id=ctx.run_id,
            task_id=node.spec.id,
            agent=node.spec.agent,
            artifacts=result.artifacts,
        )

    def _add_task(
        self,
        task: OrchestratorTask,
        nodes: dict[str, _Node],
        ctx: AgentContext,
    ) -> bool:
        if task.id in nodes:
            return False
        if task.agent not in self.agents:
            self.logger.warning("agent_dynamic_task_unknown", agent=task.agent, task_id=task.id)
            return False
        if task.agent in ctx.forbidden_agents:
            self.logger.warning(
                "agent_dynamic_task_forbidden",
                agent=task.agent,
                task_id=task.id,
                phase=ctx.phase,
            )
            return False
        if (
            ctx.allowed_dynamic_agents
            and task.agent not in ctx.allowed_dynamic_agents
        ):
            self.logger.warning(
                "agent_dynamic_task_not_in_phase",
                agent=task.agent,
                task_id=task.id,
                phase=ctx.phase,
            )
            return False
        if any(node.spec.agent == task.agent for node in nodes.values()):
            self.logger.warning(
                "agent_dynamic_task_duplicate_agent",
                agent=task.agent,
                task_id=task.id,
            )
            return False
        if any(dependency not in nodes for dependency in task.depends_on):
            self.logger.warning(
                "agent_dynamic_task_bad_dependency",
                task_id=task.id,
                depends_on=task.depends_on,
            )
            return False
        nodes[task.id] = _Node(spec=task)
        ctx.mark_agent(task.agent)
        ctx.mark_dynamic_task(task.id)
        self.logger.info(
            "agent_task_added",
            run_id=ctx.run_id,
            task_id=task.id,
            agent=task.agent,
            depends_on=task.depends_on,
        )
        return True
