"""人脸锚点智能体。"""

from __future__ import annotations

from smartcut.errors import RunCancelledError
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class FacesAgent:
    name = "faces"
    description = "OpenCV Haar 人脸检测，为 9:16 提供锚点"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        ctx.raise_if_cancelled()
        try:
            result = ctx.toolkit.detect_faces(
                ctx.project_id,
                cancel_token=ctx.cancel_token,
                chapter_ids=ctx.chapter_ids,
            )
        except RunCancelledError:
            raise
        except Exception as exc:
            return AgentResult(
                summary="人脸检测不可用，9:16 将使用中心裁切",
                output={"available": False, "error": str(exc)},
                warnings=[f"人脸检测失败: {type(exc).__name__}: {exc}"],
                confidence=0.2,
            )
        if not result.get("available", True):
            return AgentResult(
                summary="人脸检测跳过",
                output=result,
                warnings=[str(result.get("warning") or "人脸检测不可用")],
                confidence=0.2,
            )
        ctx.set_faces(result)
        return AgentResult(
            summary=f"检测到 {result.get('face_frame_count', 0)} 帧人脸",
            output={
                "available": True,
                "face_frame_count": result.get("face_frame_count", 0),
                "analyzed_frames": result.get("analyzed_frames", 0),
                "dropped_frames": result.get("dropped_frames", 0),
                "engine": result.get("engine", "thread"),
                "duration_ms": result.get("duration_ms", 0),
                "cached": result.get("cached", False),
            },
            confidence=0.75,
        )
