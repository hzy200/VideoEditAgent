"""候选片段智能体：转写句群 + 镜头/质量/人脸融合，供检索与规划使用。"""

from __future__ import annotations

from smartcut.errors import RunCancelledError
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class CandidateAgent:
    name = "candidates"
    description = "生成候选片段并建立 FTS5 检索索引"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        ctx.raise_if_cancelled()
        chapter_ids = ctx.chapter_ids
        try:
            result = ctx.toolkit.build_candidates(
                ctx.project_id,
                cancel_token=ctx.cancel_token,
                chapter_ids=chapter_ids,
            )
        except RunCancelledError:
            raise
        except Exception as exc:
            return AgentResult(
                summary="候选片段生成失败，规划改为直接读取转写",
                output={"available": False, "error": str(exc)},
                warnings=[f"候选片段生成失败: {type(exc).__name__}: {exc}"],
                confidence=0.2,
            )
        if not result.get("available"):
            return AgentResult(
                summary="候选片段生成跳过",
                output=result,
                warnings=[str(result.get("warning") or "候选片段不可用")],
                confidence=0.2,
            )
        ctx.set_candidates(result)
        return AgentResult(
            summary=f"生成 {result.get('candidate_count', 0)} 个候选片段",
            output={
                "available": True,
                "candidate_count": result.get("candidate_count", 0),
                "cached": result.get("cached", False),
            },
            confidence=0.85,
        )
