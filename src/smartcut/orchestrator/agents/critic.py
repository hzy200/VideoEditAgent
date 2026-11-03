"""Critic 智能体：按质量标记与镜头结构局部修正规划，必要时提交新 revision。"""

from __future__ import annotations

from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.quality import QualityReport
from smartcut.contracts.shots import ShotList
from smartcut.errors import RunCancelledError
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.critic import repair_sequence
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class CriticAgent:
    name = "critic"
    description = "移除质量异常区间、按镜头拆分过长片段，并提交修正版本"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        ctx.raise_if_cancelled()
        ir = ctx.shared.ir
        if ir is None:
            return AgentResult(
                summary="缺少规划，Critic 跳过",
                warnings=["Critic 缺少前置 revision"],
                confidence=0.2,
            )

        perception = ctx.toolkit.get_perception(ctx.project_id)
        quality = None
        if perception.get("quality"):
            quality = QualityReport.model_validate(perception["quality"])
        shots = None
        if perception.get("shots"):
            shots = ShotList.model_validate(perception["shots"])

        try:
            report = repair_sequence(
                ir,
                quality,
                shots,
                max_segment_seconds=ctx.settings.critic_max_segment_seconds,
                padding=ctx.settings.critic_quality_padding,
            )
        except RunCancelledError:
            raise
        except Exception as exc:
            return AgentResult(
                summary="Critic 修复失败，保留原规划",
                warnings=[f"Critic 修复失败: {type(exc).__name__}: {exc}"],
                confidence=0.3,
            )

        timeline_warnings = _timeline_warnings(ctx)

        if not report.changed:
            return AgentResult(
                summary="Critic 未发现需要修正的片段",
                output={
                    "changed": False,
                    "revision": ctx.shared.revision,
                    "segments": len(ir.sequence),
                    "changes": report.changes,
                },
                warnings=[*report.warnings, *timeline_warnings],
                confidence=0.8,
            )

        try:
            submitted = ctx.toolkit.submit_revision(ctx.project_id, report.ir)
            revision = int(submitted["revision"])
            repaired = EditIR.model_validate(submitted["ir"])
        except RunCancelledError:
            raise
        except Exception as exc:
            return AgentResult(
                summary="Critic 修正版本提交失败，保留原规划",
                warnings=[f"Critic 提交失败: {type(exc).__name__}: {exc}"],
                confidence=0.3,
            )

        ctx.set_plan(revision, repaired)
        return AgentResult(
            summary=(
                f"Critic 提交 revision {revision}："
                f"{len(ir.sequence)} → {len(repaired.sequence)} 段"
            ),
            output={
                "changed": True,
                "previous_revision": ir.revision,
                "revision": revision,
                "segments": len(repaired.sequence),
                "changes": report.changes,
            },
            warnings=[*report.warnings, *timeline_warnings],
            confidence=0.75,
        )


def _timeline_warnings(ctx: AgentContext) -> list[str]:
    """结构性问题用规则提示，覆盖度本身不当作错误（精剪天然覆盖率低）。"""
    timeline = ctx.shared.timeline
    if timeline is None:
        return []
    if timeline.duplicate_ratio > 0.2:
        return [
            f"时间线重复区间占比 {timeline.duplicate_ratio:.1%}，"
            "同一源区间被多次使用，请确认是否为有意重复"
        ]
    return []
