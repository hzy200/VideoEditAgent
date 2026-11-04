"""反思式重规划智能体：把 QA 问题转成一次新 revision。

约束：
- 只读 QA 的 review 与当前 revision，不直接改时间线；
- 必须生成**新 revision**，不修改旧 revision；
- 任何失败都降级为“保留原规划”，不得阻断后续编译/渲染。
"""

from __future__ import annotations

from smartcut.contracts.edit_ir import EditIR
from smartcut.errors import RunCancelledError
from smartcut.orchestrator.context import AgentContext, accepts_keyword
from smartcut.orchestrator.models import AgentResult, OrchestratorTask

_BAD_SEVERITIES = {"warning", "error"}


class ReplanAgent:
    name = "replan"
    description = "按 QA 问题重规划并提交新 revision（失败保留原规划）"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        ctx.raise_if_cancelled()
        if not ctx.settings.replan_enabled:
            return AgentResult(
                summary="反思式重规划已关闭",
                output={"changed": False, "reason": "disabled"},
                confidence=0.5,
            )
        # 手工 IR 是用户明确意图，规划器不得覆盖。
        if ctx.manual_ir is not None:
            return AgentResult(
                summary="手工 IR 不参与自动重规划",
                output={"changed": False, "reason": "manual_ir"},
                confidence=0.5,
            )
        review = ctx.shared.review
        revision = ctx.shared.revision
        if not review or revision is None:
            return AgentResult(
                summary="缺少审片结果或规划，跳过重规划",
                output={"changed": False, "reason": "no_review"},
                confidence=0.3,
            )

        issues = [
            item
            for item in (review.get("issues") or [])
            if isinstance(item, dict)
            and item.get("severity") in _BAD_SEVERITIES
        ]
        score = float(review.get("score") or 0.0)
        if not issues or score >= ctx.settings.replan_score_threshold:
            return AgentResult(
                summary=(
                    f"QA score={score:.2f}，无需重规划"
                    f"（{len(issues)} 个 warning/error）"
                ),
                output={
                    "changed": False,
                    "reason": "score_ok",
                    "score": score,
                    "issue_count": len(issues),
                },
                confidence=0.7,
            )

        replanner = getattr(ctx.toolkit, "replan", None)
        if not callable(replanner):
            return AgentResult(
                summary="当前工具集不支持重规划，跳过",
                output={"changed": False, "reason": "unsupported"},
                confidence=0.3,
            )

        if ctx.pinned_effects is not None and not accepts_keyword(
            replanner, "pinned_effects"
        ):
            return AgentResult(
                summary="当前工具集不支持锁定特效，跳过重规划",
                output={"changed": False, "reason": "pinned_unsupported"},
                warnings=["当前工具集不支持 pinned_effects，已保留原规划"],
                confidence=0.3,
            )

        kwargs: dict = {"review": review}
        if accepts_keyword(replanner, "cancel_token"):
            kwargs["cancel_token"] = ctx.cancel_token
        if accepts_keyword(replanner, "usage_scope"):
            kwargs["usage_scope"] = ctx.run_id
        if accepts_keyword(replanner, "token_budget"):
            kwargs["token_budget"] = ctx.settings.llm_token_budget_per_run or None
        if ctx.pinned_effects is not None and accepts_keyword(
            replanner, "pinned_effects"
        ):
            kwargs["pinned_effects"] = ctx.pinned_effects
        try:
            result = replanner(
                ctx.project_id,
                revision,
                ctx.goal,
                **kwargs,
            )
        except RunCancelledError:
            raise
        except Exception as exc:
            return AgentResult(
                summary="重规划失败，保留原 revision",
                output={
                    "changed": False,
                    "reason": "failed",
                    "previous_revision": revision,
                },
                warnings=[f"重规划失败: {type(exc).__name__}: {exc}"],
                confidence=0.4,
            )

        source = str(result.get("source") or "llm")
        if not result.get("changed", True):
            return AgentResult(
                summary=(
                    "重规划未产生变化，保留原 revision "
                    f"{revision}（{source}）"
                ),
                output={
                    "changed": False,
                    "reason": "unchanged",
                    "previous_revision": revision,
                    "revision": revision,
                    "issue_count": len(issues),
                    "score": score,
                    "source": source,
                },
                confidence=0.6,
            )
        new_revision = int(result["revision"])
        ir = EditIR.model_validate(result["ir"])
        ctx.set_plan(new_revision, ir)
        return AgentResult(
            summary=(
                f"按 QA {len(issues)} 个问题重规划："
                f"revision {revision} → {new_revision}"
            ),
            output={
                "changed": True,
                "previous_revision": revision,
                "revision": new_revision,
                "issue_count": len(issues),
                "score": score,
                "segments": len(ir.sequence),
                "source": source,
                "effects_source": (
                    "pinned" if ctx.pinned_effects is not None else source
                ),
                "pinned_effect_count": len(ctx.pinned_effects or []),
                "usage": result.get("usage") or {},
                "metrics": result.get("metrics") or {},
            },
            confidence=0.7,
        )
