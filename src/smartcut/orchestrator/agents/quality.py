"""帧质量智能体。"""

from __future__ import annotations

from smartcut.errors import RunCancelledError
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class QualityAgent:
    name = "quality"
    description = "OpenCV 黑帧/静帧/糊帧/重复帧"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        ctx.raise_if_cancelled()
        try:
            result = ctx.toolkit.analyze_quality(
                ctx.project_id,
                cancel_token=ctx.cancel_token,
                chapter_ids=ctx.chapter_ids,
            )
        except RunCancelledError:
            raise
        except Exception as exc:
            return AgentResult(
                summary="质量分析不可用，已降级",
                output={"available": False, "error": str(exc)},
                warnings=[f"质量分析失败: {type(exc).__name__}: {exc}"],
                confidence=0.2,
            )
        if not result.get("available", True):
            return AgentResult(
                summary="质量分析跳过",
                output=result,
                warnings=[str(result.get("warning") or "质量分析不可用")],
                confidence=0.2,
            )
        ctx.set_quality(result)
        return AgentResult(
            summary=f"质量标记 {result.get('mark_count', 0)} 个",
            output={
                "available": True,
                "mark_count": result.get("mark_count", 0),
                "by_kind": result.get("by_kind", {}),
                "analyzed_frames": result.get("analyzed_frames", 0),
                "dropped_frames": result.get("dropped_frames", 0),
                "engine": result.get("engine", "thread"),
                "duration_ms": result.get("duration_ms", 0),
                "cached": result.get("cached", False),
            },
            confidence=0.85,
        )
