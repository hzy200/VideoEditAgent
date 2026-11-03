"""编译智能体：把 EditIR 纯函数 lowering 为 ToolCall[]。"""

from __future__ import annotations

from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class CompileAgent:
    name = "compile"
    description = "把 EditIR 编译为受控 ToolCall[]（dry-run 计划）"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        ctx.raise_if_cancelled()
        revision = ctx.shared.revision
        if revision is None:
            raise RuntimeError("compile 缺少前置 revision")

        plan = ctx.toolkit.dry_run(ctx.project_id, revision)
        tool_calls = plan["tool_calls"]
        ctx.set_tool_calls(tool_calls)

        new_tasks: list[OrchestratorTask] = []
        if ctx.allow_render and not ctx.has_agent("render"):
            new_tasks.append(
                OrchestratorTask(
                    id="render_auto",
                    agent="render",
                    depends_on=[task.id],
                    description="编译完成后动态补充渲染任务",
                )
            )

        return AgentResult(
            summary=f"编译完成：{len(tool_calls)} 个工具调用",
            output={
                "revision": revision,
                "output_path": plan["output_path"],
                "tool_calls": tool_calls,
            },
            new_tasks=new_tasks,
            confidence=0.95,
        )
