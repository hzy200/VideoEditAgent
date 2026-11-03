"""渲染智能体：执行 ToolCall[]，产出成片。"""

from __future__ import annotations

from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class RenderAgent:
    name = "render"
    description = "执行白名单工具链，输出成片并做 ffprobe 断言"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        ctx.raise_if_cancelled()
        revision = ctx.shared.revision
        if revision is None:
            raise RuntimeError("render 缺少前置 revision")

        result = ctx.toolkit.render(ctx.project_id, revision, ctx.reporter)
        ctx.set_render(result)

        new_tasks: list[OrchestratorTask] = []
        if not ctx.has_agent("verify"):
            new_tasks.append(
                OrchestratorTask(
                    id="verify_auto",
                    agent="verify",
                    depends_on=[task.id],
                    description="渲染完成后动态补充独立校验任务",
                )
            )

        facts = result.get("facts") or {}
        output = {
            "revision": revision,
            "output_path": result["output_path"],
            "output_abs": result["output_abs"],
            "duration": facts.get("duration"),
            "tool_count": len(result.get("tool_calls", [])),
            "captions": result.get("captions", 0),
            "cache_hits": result.get("cache_hits", 0),
            "cache_misses": result.get("cache_misses", 0),
            "actual_encoder": result.get("actual_encoder"),
            "encoders_used": result.get("encoders_used") or [],
            "warnings": result.get("warnings", []),
        }
        artifacts = [result["output_path"]]
        for key in ("caption_srt_path", "caption_vtt_path"):
            if result.get(key):
                artifacts.append(result[key])
        return AgentResult(
            summary=f"渲染完成：{result['output_path']}",
            output=output,
            artifacts=artifacts,
            new_tasks=new_tasks,
            confidence=0.9,
        )
