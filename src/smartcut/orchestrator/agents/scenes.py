"""镜头切分智能体。"""

from __future__ import annotations

from smartcut.errors import RunCancelledError
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class ScenesAgent:
    name = "scenes"
    description = "PySceneDetect/OpenCV 镜头切分"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        ctx.raise_if_cancelled()
        try:
            result = ctx.toolkit.detect_scenes(
                ctx.project_id,
                cancel_token=ctx.cancel_token,
            )
        except RunCancelledError:
            raise
        except Exception as exc:
            return AgentResult(
                summary="镜头切分不可用，已降级",
                output={"available": False, "error": str(exc)},
                warnings=[f"镜头切分失败: {type(exc).__name__}: {exc}"],
                confidence=0.2,
            )
        if not result.get("available", True):
            return AgentResult(
                summary="镜头切分跳过",
                output=result,
                warnings=[str(result.get("warning") or "镜头切分不可用")],
                confidence=0.2,
            )
        ctx.set_shots(result)
        return AgentResult(
            summary=f"检测到 {result.get('shot_count', 0)} 个镜头切点",
            output={
                "available": True,
                "shot_count": result.get("shot_count", 0),
            },
            confidence=0.9,
        )
