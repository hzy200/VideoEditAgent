"""转写智能体：抽取音频并调用转写器；缺依赖时明确降级。"""

from __future__ import annotations

from smartcut.errors import RunCancelledError
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class TranscribeAgent:
    name = "transcribe"
    description = "抽取 16k 单声道音频并生成词级转写（可降级）"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        ctx.raise_if_cancelled()
        facts = ctx.shared.facts or ctx.toolkit.get_media_facts(ctx.project_id)
        ctx.set_facts(facts)

        if not facts.has_audio:
            summary = {
                "degraded": True,
                "language": None,
                "segments": 0,
                "words": 0,
                "note": "无音频轨，跳过转写",
            }
            ctx.set_transcript(None, summary)
            return AgentResult(
                summary="素材无音频，跳过转写",
                output=summary,
                warnings=["无音频轨"],
                confidence=1.0,
            )

        transcribe_error: Exception | None = None
        try:
            result = ctx.toolkit.transcribe(
                ctx.project_id,
                ctx.reporter,
                chapter_ids=ctx.chapter_ids,
            )
            transcript = ctx.toolkit.get_transcript(ctx.project_id)
        except RunCancelledError:
            raise
        except Exception as exc:
            # 转写不是阻断阶段：失败时写入 degraded 摘要，后续章节/候选/规划
            # 仍可使用媒体事实与感知结果降级运行。
            transcribe_error = exc
            result = {
                "degraded": True,
                "language": None,
                "segments": 0,
                "words": 0,
                "note": f"转写失败: {type(exc).__name__}: {exc}",
            }
            transcript = None

        ctx.raise_if_cancelled()
        ctx.set_transcript(transcript, result)

        warnings: list[str] = []
        if transcribe_error is not None:
            warnings.append(
                f"转写失败，已降级为无转写上下文: "
                f"{type(transcribe_error).__name__}: {transcribe_error}"
            )
        elif result.get("degraded"):
            note = result.get("note") or "转写器不可用"
            warnings.append(f"转写降级：{note}")

        new_tasks: list[OrchestratorTask] = []
        can_plan = ctx.allow_render and (
            ctx.toolkit.planner_available or ctx.manual_ir is not None
        )
        if can_plan and not ctx.has_agent("plan"):
            new_tasks.append(
                OrchestratorTask(
                    id="plan_auto",
                    agent="plan",
                    depends_on=[task.id],
                    description="转写完成后动态补充规划任务",
                )
            )

        if transcribe_error is not None:
            summary = "转写失败，已降级为无转写上下文"
            confidence = 0.2
        else:
            summary = (
                f"转写完成：{result.get('segments', 0)} 句 / "
                f"{result.get('words', 0)} 词"
                + ("（降级）" if result.get("degraded") else "")
            )
            confidence = 0.5 if result.get("degraded") else 0.9
        return AgentResult(
            summary=summary,
            output={**result, "error": None if transcribe_error is None else str(transcribe_error)},
            warnings=warnings,
            new_tasks=new_tasks,
            confidence=confidence,
        )
