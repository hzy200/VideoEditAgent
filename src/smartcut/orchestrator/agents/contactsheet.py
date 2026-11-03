"""Contact sheet 智能体。"""

from __future__ import annotations

from smartcut.errors import RunCancelledError
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class ContactSheetAgent:
    name = "contactsheet"
    description = "Pillow 拼接触表，供 GUI/多图理解"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        ctx.raise_if_cancelled()
        try:
            result = ctx.toolkit.build_contact_sheet(
                ctx.project_id,
                cancel_token=ctx.cancel_token,
            )
        except RunCancelledError:
            raise
        except Exception as exc:
            return AgentResult(
                summary="contact sheet 生成失败，已降级",
                output={"available": False, "error": str(exc)},
                warnings=[f"contact sheet 失败: {type(exc).__name__}: {exc}"],
                confidence=0.2,
            )
        if not result.get("available", True):
            return AgentResult(
                summary="contact sheet 跳过",
                output=result,
                warnings=[str(result.get("warning") or "没有可用抽帧")],
                confidence=0.2,
            )
        ctx.set_contact_sheet(result)
        return AgentResult(
            summary=f"contact sheet 完成：{result.get('path')}",
            output={
                "available": True,
                "path": result.get("path"),
                "count": result.get("count", 0),
                "image_bytes": result.get("image_bytes", 0),
                "duration_ms": result.get("duration_ms", 0),
                "cached": result.get("cached", False),
            },
            artifacts=[result["path"]] if result.get("path") else [],
            confidence=0.9,
        )
