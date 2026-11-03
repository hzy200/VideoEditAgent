"""素材分析智能体：读取 MediaFacts，标记风险，并在需要时补充转写任务。"""

from __future__ import annotations

from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class AnalyzeAgent:
    name = "analyze"
    description = "读取 MediaFacts，标记 VFR/rotation/无音频等风险"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        facts = ctx.toolkit.get_media_facts(ctx.project_id)
        ctx.set_facts(facts)

        warnings: list[str] = []
        if facts.vfr:
            warnings.append("检测到 VFR，P1 未做帧率统一")
        if facts.rotation not in (0, 180):
            warnings.append(f"素材 rotation={facts.rotation}，P1 未做转置")
        if not facts.has_audio:
            warnings.append("素材没有音频轨，将跳过转写")
        if facts.color_transfer and facts.color_transfer.lower() not in {"bt709", "sdr"}:
            warnings.append(f"色彩传输 {facts.color_transfer} 可能不是 SDR/Rec.709")

        new_tasks: list[OrchestratorTask] = []
        if facts.video is not None and not ctx.has_agent("frames"):
            new_tasks.append(
                OrchestratorTask(
                    id="frames_auto",
                    agent="frames",
                    depends_on=[task.id],
                    description="检测到视频，动态补充抽帧任务",
                )
            )
        if (
            facts.has_audio
            and ctx.phase != "fast_outline"
            and not ctx.has_agent("transcribe")
        ):
            new_tasks.append(
                OrchestratorTask(
                    id="transcribe_auto",
                    agent="transcribe",
                    description="检测到音频，动态补充转写任务",
                )
            )

        output = {
            "src": facts.src,
            "duration_seconds": facts.duration.to_ffmpeg_seconds(),
            "fps": facts.fps,
            "width": facts.width,
            "height": facts.height,
            "has_audio": facts.has_audio,
            "vfr": facts.vfr,
            "rotation": facts.rotation,
        }
        return AgentResult(
            summary=f"素材分析完成：{facts.duration.to_ffmpeg_seconds()}s / {facts.width}x{facts.height}",
            output=output,
            warnings=warnings,
            new_tasks=new_tasks,
            confidence=1.0,
        )
