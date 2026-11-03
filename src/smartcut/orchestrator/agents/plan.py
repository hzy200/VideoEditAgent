"""规划智能体：调用 DeepSeek 或提交手工 IR，产出 EditIR revision。"""

from __future__ import annotations

from smartcut.contracts.edit_ir import EditIR
from smartcut.orchestrator.context import AgentContext, accepts_keyword
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class PlanAgent:
    name = "plan"
    description = "基于 MediaFacts 与转写生成 EditIR（或提交手工 IR）"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        ctx.raise_if_cancelled()
        perception = ctx.toolkit.get_perception(ctx.project_id)
        chapters = ctx.shared.chapters or ctx.toolkit.get_chapters(ctx.project_id)
        candidates = ctx.shared.candidates or ctx.toolkit.get_candidates(ctx.project_id)
        chapter_ids = ctx.chapter_ids
        if ctx.manual_ir is not None:
            result = ctx.toolkit.submit_revision(ctx.project_id, ctx.manual_ir)
            source = "manual"
        else:
            if candidates:
                retrieved = ctx.toolkit.retrieve_candidates(ctx.project_id, ctx.goal)
                if retrieved.get("candidates"):
                    candidates = retrieved
            plan_kwargs = {
                "perception": perception,
                "chapters": chapters,
                "candidates": candidates,
                "chapter_ids": chapter_ids,
            }
            if ctx.shared.timeline is not None and accepts_keyword(
                ctx.toolkit.plan, "timeline"
            ):
                plan_kwargs["timeline"] = ctx.shared.timeline
            if accepts_keyword(ctx.toolkit.plan, "cancel_token"):
                plan_kwargs["cancel_token"] = ctx.cancel_token
            if accepts_keyword(ctx.toolkit.plan, "usage_scope"):
                plan_kwargs["usage_scope"] = ctx.run_id
            if accepts_keyword(ctx.toolkit.plan, "token_budget"):
                plan_kwargs["token_budget"] = (
                    ctx.settings.llm_token_budget_per_run or None
                )
            if ctx.pinned_effects is not None and accepts_keyword(
                ctx.toolkit.plan, "pinned_effects"
            ):
                plan_kwargs["pinned_effects"] = ctx.pinned_effects
            result = ctx.toolkit.plan(
                ctx.project_id,
                ctx.goal,
                **plan_kwargs,
            )
            source = "llm"

        revision = int(result["revision"])
        ir = EditIR.model_validate(result["ir"])
        ctx.set_plan(revision, ir)

        confidences = [
            op.confidence for op in ir.sequence if op.confidence is not None
        ]
        confidence = (
            sum(confidences) / len(confidences) if confidences else 0.8
        )

        warnings: list[str] = []
        transcript_summary = ctx.shared.transcript_summary
        if transcript_summary and transcript_summary.get("degraded"):
            warnings.append("转写降级，选段依据仅限媒体事实")
        if not perception.get("available"):
            warnings.append("视觉感知不可用，规划仅基于媒体事实/转写")
        if not chapters or not chapters.get("chapters"):
            warnings.append("章节计划不可用，规划使用全量上下文")
        if not candidates or not candidates.get("candidates"):
            warnings.append("候选片段不可用，规划改为直接读取转写")

        new_tasks: list[OrchestratorTask] = []
        if ctx.allow_render and not ctx.has_agent("compile"):
            new_tasks.append(
                OrchestratorTask(
                    id="compile_auto",
                    agent="compile",
                    depends_on=[task.id],
                    description="规划完成后动态补充编译任务",
                )
            )

        return AgentResult(
            summary=f"生成 revision {revision}（{source}），保留 {len(ir.sequence)} 段",
            output={
                "revision": revision,
                "segments": len(ir.sequence),
                "source": source,
                "effects_source": (
                    "pinned" if ctx.pinned_effects is not None else source
                ),
                "pinned_effect_count": len(ctx.pinned_effects or []),
                "expected_duration": ir.total_duration().to_ffmpeg_seconds(),
                "perception_available": bool(perception.get("available")),
                "chapter_count": len(chapters.get("chapters") or []) if chapters else 0,
                "candidate_count": len(candidates.get("candidates") or []) if candidates else 0,
                "usage": result.get("usage") or {},
                "metrics": result.get("metrics") or {},
                "memory_used": bool(result.get("memory_used")),
                "history_runs": int(result.get("history_runs") or 0),
                "preference_used": bool(result.get("preference_used")),
                "timeline": (
                    {
                        "coverage_ratio": round(ctx.shared.timeline.coverage_ratio, 4),
                        "duplicate_ratio": round(ctx.shared.timeline.duplicate_ratio, 4),
                        "output_seconds": ctx.shared.timeline.output_duration.to_ffmpeg_seconds(),
                    }
                    if ctx.shared.timeline is not None
                    else None
                ),
            },
            warnings=warnings,
            new_tasks=new_tasks,
            confidence=min(max(confidence, 0.0), 1.0),
        )
