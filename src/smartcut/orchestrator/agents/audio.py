"""音频智能体：按词级时间戳去除口癖，必要时提交新 revision。"""

from __future__ import annotations

from smartcut.contracts.edit_ir import EditIR
from smartcut.errors import RunCancelledError
from smartcut.orchestrator.audio import repair_audio
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class AudioAgent:
    name = "audio"
    description = "基于词级转写去掉口癖并提交修正版本"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        ctx.raise_if_cancelled()
        ir = ctx.shared.ir
        if ir is None:
            return AgentResult(
                summary="缺少规划，AudioAgent 跳过",
                warnings=["AudioAgent 缺少前置 revision"],
                confidence=0.2,
            )
        if not ir.audio.remove_filler:
            return AgentResult(
                summary="未请求去口癖，音频保持不变",
                output={"changed": False, "revision": ctx.shared.revision},
                confidence=0.8,
            )

        transcript = ctx.shared.transcript or ctx.toolkit.get_transcript(ctx.project_id)
        report = repair_audio(
            ir,
            transcript,
            filler_words=ctx.settings.audio_filler_set,
            padding=ctx.settings.audio_filler_padding,
            min_segment_seconds=ctx.settings.audio_min_segment_seconds,
        )
        if not report.changed:
            return AgentResult(
                summary="未发现需要删除的口癖",
                output={
                    "changed": False,
                    "revision": ctx.shared.revision,
                    "removed_words": report.removed_words,
                    "changes": report.changes,
                },
                warnings=report.warnings,
                confidence=0.75,
            )

        try:
            submitted = ctx.toolkit.submit_revision(ctx.project_id, report.ir)
            revision = int(submitted["revision"])
            repaired = EditIR.model_validate(submitted["ir"])
        except RunCancelledError:
            raise
        except Exception as exc:
            return AgentResult(
                summary="口癖修复版本提交失败，保留原规划",
                warnings=[f"AudioAgent 提交失败: {type(exc).__name__}: {exc}"],
                confidence=0.3,
            )

        ctx.set_plan(revision, repaired)
        return AgentResult(
            summary=(
                f"AudioAgent 提交 revision {revision}：移除 "
                f"{report.removed_words} 个口癖词"
            ),
            output={
                "changed": True,
                "previous_revision": ir.revision,
                "revision": revision,
                "removed_words": report.removed_words,
                "changes": report.changes,
            },
            warnings=report.warnings,
            confidence=0.75,
        )
