"""语义审片智能体：对规划做 LLM/规则审查，不直接修改时间线。"""

from __future__ import annotations

from smartcut.errors import RunCancelledError
from smartcut.orchestrator.context import AgentContext, accepts_keyword
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class SemanticQAAgent:
    name = "qa"
    description = "对 EditIR 做语义/结构审片，输出问题与置信度"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        ctx.raise_if_cancelled()
        revision = ctx.shared.revision
        if revision is None:
            return AgentResult(
                summary="缺少规划，语义 QA 跳过",
                warnings=["语义 QA 缺少前置 revision"],
                confidence=0.2,
            )
        try:
            review_kwargs: dict = {"visual_notes": None}
            if ctx.shared.timeline is not None and accepts_keyword(
                ctx.toolkit.review_edit, "timeline"
            ):
                review_kwargs["timeline"] = ctx.shared.timeline
            if accepts_keyword(ctx.toolkit.review_edit, "cancel_token"):
                review_kwargs["cancel_token"] = ctx.cancel_token
            if accepts_keyword(ctx.toolkit.review_edit, "usage_scope"):
                review_kwargs["usage_scope"] = ctx.run_id
            if accepts_keyword(ctx.toolkit.review_edit, "token_budget"):
                review_kwargs["token_budget"] = (
                    ctx.settings.llm_token_budget_per_run or None
                )
            review = ctx.toolkit.review_edit(
                ctx.project_id,
                revision,
                ctx.goal,
                **review_kwargs,
            )
        except RunCancelledError:
            raise
        except Exception as exc:
            return AgentResult(
                summary="语义 QA 失败，跳过",
                warnings=[f"语义 QA 失败: {type(exc).__name__}: {exc}"],
                confidence=0.3,
            )

        ctx.set_review(review)
        issues = review.get("issues") or []
        warnings = [
            f"QA[{item.get('severity', 'warning')}] {item.get('message', '')}".strip()
            for item in issues
            if item.get("severity") in {"warning", "error"}
        ]
        score = float(review.get("score") or 0.0)
        return AgentResult(
            summary=(
                f"语义 QA score={score:.2f}，"
                f"{len(issues)} 个问题（{review.get('source', 'rules')}）"
            ),
            output={
                "revision": revision,
                "score": score,
                "issue_count": len(issues),
                "source": review.get("source"),
                "summary": review.get("summary", ""),
                "usage": review.get("usage") or {},
            },
            warnings=warnings,
            confidence=max(0.0, min(1.0, score)),
        )
