"""Orchestrator 门面：构建任务图 → 并行调度 → 最终决策。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.effects import Effect
from smartcut.llm.client import DeepSeekClient
from smartcut.logging import get_logger
from smartcut.orchestrator.agents import build_default_agents
from smartcut.orchestrator.base import SubAgent
from smartcut.orchestrator.context import (
    AgentContext,
    AgentToolkit,
    CancellableToolkit,
)
from smartcut.orchestrator.decision import DecisionAgent
from smartcut.orchestrator.graph import TaskGraphExecutor
from smartcut.orchestrator.models import OrchestrationResult, TaskStatus
from smartcut.orchestrator.planner import TaskPlanner
from smartcut.policy import load_policy

_POLICY = load_policy()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Orchestrator:
    def __init__(
        self,
        toolkit: AgentToolkit,
        settings: Settings,
        *,
        agents: dict[str, SubAgent],
        task_planner: TaskPlanner,
        decision: DecisionAgent,
        logger: Any | None = None,
    ) -> None:
        self.toolkit = toolkit
        self.settings = settings
        self.agents = agents
        self.task_planner = task_planner
        self.decision = decision
        self.logger = logger or get_logger("smartcut.orchestrator")

    def run(
        self,
        *,
        run_id: str,
        project_id: str,
        goal: str,
        allow_render: bool = True,
        manual_ir: EditIR | None = None,
        pinned_effects: list[Effect] | None = None,
        phase: str = "plan",
        approved_outline: Any | None = None,
        reporter: Any | None = None,
        cancel_token: Any | None = None,
        on_task_complete: Any | None = None,
    ) -> OrchestrationResult:
        started_at = _now()
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        workspace = self.toolkit.project_workspace(project_id)

        plan = self.task_planner.build(
            project_id,
            goal,
            allow_render=allow_render,
            manual_ir=manual_ir,
            phase=phase,
            run_id=run_id,
            cancel_token=cancel_token,
        )
        self.logger.info(
            "orchestration_start",
            run_id=run_id,
            project_id=project_id,
            tasks=[task.id for task in plan.tasks],
            rationale=plan.rationale,
        )

        chapter_ids = (
            [chapter.id for chapter in approved_outline.chapters]
            if approved_outline is not None
            else None
        )
        phase_policy = _POLICY.workflow.phases.get(phase)
        forbidden_agents = frozenset(
            phase_policy.forbidden if phase_policy is not None else ()
        )
        allowed_dynamic_agents = self.task_planner.allowed_dynamic_agents(
            project_id,
            allow_render=allow_render,
            manual_ir=manual_ir,
            phase=phase,
        )
        fenced_toolkit = (
            CancellableToolkit(self.toolkit, cancel_token)
            if cancel_token is not None
            else self.toolkit
        )
        ctx = AgentContext(
            run_id=run_id,
            project_id=project_id,
            goal=goal,
            toolkit=fenced_toolkit,
            workspace=workspace,
            settings=self.settings,
            allow_render=allow_render,
            manual_ir=manual_ir,
            pinned_effects=pinned_effects,
            phase=phase,
            approved_outline=approved_outline,
            reporter=reporter,
            cancel_token=cancel_token,
            chapter_ids=chapter_ids,
            forbidden_agents=forbidden_agents,
            allowed_dynamic_agents=allowed_dynamic_agents,
        )
        executor = TaskGraphExecutor(
            self.agents,
            max_parallel=self.settings.orchestrator_max_parallel,
            max_rounds=self.settings.orchestrator_max_rounds,
            logger=self.logger,
        )
        ledger, all_ok = executor.run(plan, ctx, on_task_complete)

        succeeded = [item for item in ledger if item.status == TaskStatus.succeeded]
        cancelled = [item for item in ledger if item.status == TaskStatus.cancelled]
        if all_ok and phase in {"outline", "fast_outline"}:
            status = "awaiting_approval"
        elif all_ok:
            status = "succeeded"
        elif cancelled:
            status = "cancelled"
        elif succeeded:
            status = "partial"
        else:
            status = "failed"

        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        decision = self.decision.decide(
            ctx,
            ledger,
            plan,
            status,
            cancel_token=cancel_token,
        )
        finished_at = _now()
        usage_getter = getattr(self.toolkit, "llm_usage", None)
        llm_usage = usage_getter(run_id) if callable(usage_getter) else {}
        metrics = self._collect_metrics(
            ledger,
            started_at,
            finished_at,
            ctx.dynamic_tasks(),
            llm_usage,
        )
        result = OrchestrationResult(
            run_id=run_id,
            project_id=project_id,
            goal=goal,
            status=status,
            answer=decision.answer,
            confidence=decision.confidence,
            rationale=plan.rationale,
            plan=plan,
            ledger=ledger,
            metrics=metrics,
            artifacts=decision.artifacts,
            warnings=decision.warnings,
            started_at=started_at,
            finished_at=finished_at,
        )
        self.logger.info(
            "orchestration_done",
            run_id=run_id,
            status=status,
            confidence=result.confidence,
            artifacts=result.artifacts,
        )
        return result


    @staticmethod
    def _collect_metrics(
        ledger: list,
        started_at: datetime,
        finished_at: datetime,
        dynamic_tasks: list[str] | None = None,
        llm_usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tasks: list[dict[str, Any]] = []
        totals = {
            "queue_wait_ms": 0,
            "duration_ms": 0,
            "retries": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "llm_requests": 0,
            "llm_cache_hits": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "vlm_images": 0,
            "vlm_image_bytes": 0,
            "vlm_cache_hits": 0,
            "frames_extracted": 0,
            "frames_analyzed": 0,
            "frames_dropped": 0,
            "planner_context_tokens": 0,
            "stage_durations_ms": {},
        }
        by_agent: dict[str, int] = {}
        encoders: set[str] = set()
        for outcome in ledger:
            output = outcome.output or {}
            cache_hits = output.get("cache_hits")
            cache_misses = output.get("cache_misses")
            if cache_hits is None and cache_misses is None and "cached" in output:
                cache_hits = 1 if output.get("cached") else 0
                cache_misses = 0 if output.get("cached") else 1
            cache_hits = int(cache_hits or 0)
            cache_misses = int(cache_misses or 0)
            usage = output.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            metrics = output.get("metrics")
            if not isinstance(metrics, dict):
                metrics = {}
            vision = metrics.get("vision")
            if not isinstance(vision, dict):
                vision = {}
            stages = metrics.get("stages")
            if not isinstance(stages, dict):
                stages = {}
            context = metrics.get("context")
            if not isinstance(context, dict):
                context = {}
            context_tokens = int(context.get("estimated_tokens") or 0)
            encoder = output.get("actual_encoder") or output.get("encoder")
            used = output.get("encoders_used") or []
            if encoder:
                encoders.add(str(encoder))
            encoders.update(str(item) for item in used if item)
            vlm_images = int(vision.get("images") or 0)
            vlm_image_bytes = int(vision.get("image_bytes") or 0)
            vlm_cache_hits = int(vision.get("cache_hits") or 0)
            frames_extracted = int(output.get("decoded_frames") or 0)
            frames_analyzed = int(output.get("analyzed_frames") or 0)
            frames_dropped = int(output.get("dropped_frames") or 0)
            task = {
                "task_id": outcome.task_id,
                "agent": outcome.agent,
                "status": outcome.status.value
                if hasattr(outcome.status, "value")
                else str(outcome.status),
                "attempts": outcome.attempts,
                "retries": outcome.retries,
                "queue_wait_ms": outcome.queue_wait_ms,
                "duration_ms": outcome.duration_ms,
                "cache_hits": cache_hits,
                "cache_misses": cache_misses,
                "llm_requests": int(usage.get("requests") or 0),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "vlm_images": vlm_images,
                "vlm_image_bytes": vlm_image_bytes,
                "vlm_cache_hits": vlm_cache_hits,
                "frames_extracted": frames_extracted,
                "frames_analyzed": frames_analyzed,
                "frames_dropped": frames_dropped,
                "planner_context_tokens": context_tokens,
                "stage_durations_ms": {
                    str(name): int(value) for name, value in stages.items()
                },
                "encoder": encoder,
            }
            tasks.append(task)
            totals["queue_wait_ms"] += outcome.queue_wait_ms
            totals["duration_ms"] += outcome.duration_ms
            totals["retries"] += outcome.retries
            totals["cache_hits"] += cache_hits
            totals["cache_misses"] += cache_misses
            totals["llm_requests"] += int(usage.get("requests") or 0)
            totals["llm_cache_hits"] += int(usage.get("cache_hits") or 0)
            totals["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            totals["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            totals["total_tokens"] += int(usage.get("total_tokens") or 0)
            totals["vlm_images"] += vlm_images
            totals["vlm_image_bytes"] += vlm_image_bytes
            totals["vlm_cache_hits"] += vlm_cache_hits
            totals["frames_extracted"] += frames_extracted
            totals["frames_analyzed"] += frames_analyzed
            totals["frames_dropped"] += frames_dropped
            totals["planner_context_tokens"] += context_tokens
            for name, value in stages.items():
                key = str(name)
                totals["stage_durations_ms"][key] = (
                    totals["stage_durations_ms"].get(key, 0) + int(value)
                )
            by_agent[outcome.agent] = (
                by_agent.get(outcome.agent, 0) + outcome.duration_ms
            )
        cache_total = totals["cache_hits"] + totals["cache_misses"]
        totals["cache_hit_rate"] = (
            round(totals["cache_hits"] / cache_total, 4) if cache_total else 0.0
        )
        return {
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "wall_duration_ms": int((finished_at - started_at).total_seconds() * 1000),
            "tasks": tasks,
            "dynamic_tasks": dynamic_tasks or [],
            "llm_usage": llm_usage or {},
            "by_agent_duration_ms": by_agent,
            "totals": totals,
            "encoders": sorted(encoders),
        }


def build_orchestrator(
    toolkit: AgentToolkit,
    settings: Settings,
    *,
    llm: DeepSeekClient | None = None,
    logger: Any | None = None,
) -> Orchestrator:
    agents = build_default_agents()
    return Orchestrator(
        toolkit,
        settings,
        agents=agents,
        task_planner=TaskPlanner(toolkit, settings, llm, logger),
        decision=DecisionAgent(settings, llm, logger),
        logger=logger,
    )
