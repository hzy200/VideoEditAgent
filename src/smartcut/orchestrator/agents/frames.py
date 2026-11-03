"""抽帧智能体：为 OpenCV 感知准备 1fps JPEG。"""

from __future__ import annotations

from smartcut.errors import RunCancelledError
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class FramesAgent:
    name = "frames"
    description = "1fps 抽帧，供镜头/质量/人脸/contact sheet 使用"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        ctx.raise_if_cancelled()
        try:
            result = ctx.toolkit.extract_frames(
                ctx.project_id,
                cancel_token=ctx.cancel_token,
                chapter_ids=ctx.chapter_ids,
            )
        except RunCancelledError:
            raise
        except Exception as exc:
            return AgentResult(
                summary="抽帧失败，视觉感知降级",
                output={"available": False, "error": str(exc)},
                warnings=[f"抽帧失败: {type(exc).__name__}: {exc}"],
                confidence=0.2,
            )

        ctx.set_frames(result)
        if not result.get("available", True):
            return AgentResult(
                summary="跳过抽帧：素材没有视频轨",
                output=result,
                warnings=[str(result.get("warning") or "无视频轨")],
                confidence=0.2,
            )

        new_tasks: list[OrchestratorTask] = []
        dynamic_agents = [
            ("scenes", "scenes_auto", "动态补充镜头切分"),
            ("quality", "quality_auto", "动态补充质量分析"),
            ("faces", "faces_auto", "动态补充人脸检测"),
            ("contactsheet", "contactsheet_auto", "动态补充 contact sheet"),
        ]
        if ctx.phase == "fast_outline":
            dynamic_agents = [
                item for item in dynamic_agents if item[0] not in {"quality", "faces"}
            ]
        for agent, task_id, label in dynamic_agents:
            if not ctx.has_agent(agent):
                new_tasks.append(
                    OrchestratorTask(
                        id=task_id,
                        agent=agent,
                        depends_on=[task.id],
                        description=label,
                    )
                )

        return AgentResult(
            summary=f"抽帧完成：{result.get('count', 0)} 帧",
            output={
                "available": True,
                "count": result.get("count", 0),
                "decoded_frames": result.get("count", 0),
                "thumbnail_count": result.get("thumbnail_count", 0),
                "sampled_fps": result.get("sampled_fps", 1.0),
                "duration_ms": result.get("duration_ms", 0),
                "decoder": result.get("decoder", "cpu"),
                "cached": result.get("cached", False),
            },
            warnings=list(result.get("warnings") or []),
            new_tasks=new_tasks,
            confidence=0.95,
        )
