"""章节化智能体：长视频粗粒度结构。"""

from __future__ import annotations

from smartcut.errors import RunCancelledError
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class ChapterizerAgent:
    name = "chapters"
    description = "按镜头/转写/时长生成 ChapterPlan"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        ctx.raise_if_cancelled()
        if ctx.approved_outline is not None:
            chapters = [
                chapter.model_dump(mode="json")
                for chapter in ctx.approved_outline.chapters
            ]
            result = {
                "src": ctx.approved_outline.src,
                "available": True,
                "chapter_count": len(chapters),
                "chapters": chapters,
                "rationale": "使用人工确认大纲",
                "cached": True,
            }
            ctx.set_chapters(result)
            return AgentResult(
                summary=f"使用人工确认大纲：{len(chapters)} 个章节",
                output={
                    "available": True,
                    "chapter_count": len(chapters),
                    "approved": True,
                },
                confidence=0.95,
            )
        try:
            result = ctx.toolkit.build_chapters(
                ctx.project_id,
                cancel_token=ctx.cancel_token,
            )
        except RunCancelledError:
            raise
        except Exception as exc:
            return AgentResult(
                summary="章节切分失败，规划将使用全量上下文",
                output={"available": False, "error": str(exc)},
                warnings=[f"章节切分失败: {type(exc).__name__}: {exc}"],
                confidence=0.2,
            )
        if not result.get("available"):
            return AgentResult(
                summary="章节切分跳过",
                output=result,
                warnings=[str(result.get("warning") or "章节切分不可用")],
                confidence=0.2,
            )
        ctx.set_chapters(result)
        return AgentResult(
            summary=f"生成 {result.get('chapter_count', 0)} 个章节",
            output={
                "available": True,
                "chapter_count": result.get("chapter_count", 0),
                "cached": result.get("cached", False),
            },
            confidence=0.85,
        )
