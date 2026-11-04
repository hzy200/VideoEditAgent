"""最终决策智能体：汇总任务账本，输出面向用户的答案。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from smartcut.config import Settings
from smartcut.errors import RunCancelledError
from smartcut.llm.client import DeepSeekClient
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import OrchestrationPlan, TaskOutcome, TaskStatus


@dataclass
class DecisionOutcome:
    answer: str
    confidence: float
    warnings: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)


class DecisionAgent:
    def __init__(
        self,
        settings: Settings,
        llm: DeepSeekClient | None = None,
        logger: Any | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.logger = logger

    def decide(
        self,
        ctx: AgentContext,
        ledger: list[TaskOutcome],
        plan: OrchestrationPlan,
        status: str,
        *,
        cancel_token: Any | None = None,
    ) -> DecisionOutcome:
        warnings = list(ctx.shared.warnings)
        for outcome in ledger:
            for warning in outcome.warnings:
                if warning not in warnings:
                    warnings.append(warning)
        artifacts = sorted(
            {artifact for outcome in ledger for artifact in outcome.artifacts}
        )

        succeeded = sum(1 for item in ledger if item.status == TaskStatus.succeeded)
        failed = [item for item in ledger if item.status == TaskStatus.failed]
        total = len(ledger)

        answer = self._deterministic_answer(ctx, ledger, status, succeeded, total, failed)
        confidence = self._confidence(ctx, status, succeeded, total)

        if (
            self.settings.llm_decision_summary
            and self.llm is not None
            and self.llm.available
        ):
            try:
                answer = (
                    self._llm_answer(
                        ctx,
                        ledger,
                        status,
                        cancel_token=cancel_token,
                    )
                    or answer
                )
            except RunCancelledError:
                raise
            except Exception as exc:
                # 最终总结是增强项，不能因 LLM/缓存/资源异常把成功流水线判失败。
                warnings.append(f"最终决策 LLM 总结失败，已使用规则答案: {exc}")

        return DecisionOutcome(
            answer=answer,
            confidence=confidence,
            warnings=warnings,
            artifacts=artifacts,
        )

    def _deterministic_answer(
        self,
        ctx: AgentContext,
        ledger: list[TaskOutcome],
        status: str,
        succeeded: int,
        total: int,
        failed: list[TaskOutcome],
    ) -> str:
        lines = [f"Orchestrator 状态：{status}（{succeeded}/{total} 个任务成功）。"]
        facts = ctx.shared.facts
        if facts is not None:
            lines.append(
                f"素材：{facts.duration.to_ffmpeg_seconds()}s，"
                f"{facts.width}x{facts.height}，{facts.fps or '未知帧率'}，"
                f"{'有' if facts.has_audio else '无'}音频。"
            )
        else:
            lines.append("素材：未获取到有效媒体事实。")

        transcript = ctx.shared.transcript_summary
        if transcript is not None:
            state = "降级" if transcript.get("degraded") else "完成"
            lines.append(
                f"转写：{state}，{transcript.get('segments', 0)} 句 / "
                f"{transcript.get('words', 0)} 词。"
            )

        perception_bits: list[str] = []
        if ctx.shared.shots is not None:
            perception_bits.append(f"镜头切点 {ctx.shared.shots.get('shot_count', 0)} 个")
        if ctx.shared.quality is not None:
            perception_bits.append(f"质量标记 {ctx.shared.quality.get('mark_count', 0)} 个")
        if ctx.shared.faces is not None:
            perception_bits.append(f"人脸帧 {ctx.shared.faces.get('face_frame_count', 0)} 个")
        if perception_bits:
            lines.append("感知：" + "，".join(perception_bits) + "。")
        if ctx.shared.contact_sheet and ctx.shared.contact_sheet.get("path"):
            lines.append(f"Contact sheet：{ctx.shared.contact_sheet['path']}。")
        if ctx.phase in {"outline", "fast_outline"}:
            lines.append(
                "大纲已生成：请在“大纲”页确认章节（可改名/排序/取消选择），"
                "确认后将继续规划与渲染。"
            )
        if ctx.shared.candidates is not None:
            lines.append(
                f"候选片段：{ctx.shared.candidates.get('candidate_count', 0)} 个。"
            )
        plan = next((item for item in ledger if item.agent == "plan"), None)
        if plan is not None and plan.output.get("source"):
            source = str(plan.output.get("source"))
            label = {
                "llm": "AI 规划",
                "rule": "规则规划（离线，未使用 AI）",
                "manual": "手工 IR",
            }.get(source, source)
            lines.append(
                f"规划来源：{label}，revision {plan.output.get('revision')}，"
                f"保留 {plan.output.get('segments', 0)} 段。"
            )
        timeline = ctx.shared.timeline
        if timeline is not None:
            lines.append(
                f"时间线：覆盖 {timeline.coverage_ratio:.1%}，"
                f"重复 {timeline.duplicate_ratio:.1%}，"
                f"输出 {timeline.output_duration.to_ffmpeg_seconds()}s。"
            )
        critic = next((item for item in ledger if item.agent == "critic"), None)
        if critic is not None and critic.output.get("changed"):
            lines.append(
                f"Critic：revision {critic.output.get('previous_revision')} → "
                f"{critic.output.get('revision')}，"
                f"{len(critic.output.get('changes') or [])} 项局部修正。"
            )
        audio = next((item for item in ledger if item.agent == "audio"), None)
        if audio is not None and audio.output.get("changed"):
            lines.append(
                f"AudioAgent：移除 {audio.output.get('removed_words', 0)} 个口癖词，"
                f"revision {audio.output.get('previous_revision')} → "
                f"{audio.output.get('revision')}。"
            )
        replan = next((item for item in ledger if item.agent == "replan"), None)
        if replan is not None and replan.output.get("changed"):
            lines.append(
                f"重规划：按 {replan.output.get('issue_count', 0)} 个审片问题，"
                f"revision {replan.output.get('previous_revision')} → "
                f"{replan.output.get('revision')}。"
            )
        review = ctx.shared.review
        if review:
            lines.append(
                f"语义 QA：score {float(review.get('score') or 0):.2f}，"
                f"{len(review.get('issues') or [])} 个问题"
                f"（{review.get('source', 'rules')}）。"
            )

        render = ctx.shared.render_result
        if render is not None:
            lines.append(f"产出：{render.get('output_path')}。")
            if render.get("cache_hits") or render.get("cache_misses"):
                lines.append(
                    f"片段缓存：命中 {render.get('cache_hits', 0)}，"
                    f"新渲染 {render.get('cache_misses', 0)}。"
                )
            if render.get("actual_encoder"):
                lines.append(f"编码器：{render.get('actual_encoder')}。")
            render_facts = render.get("facts") or {}
            duration = render_facts.get("duration")
            if isinstance(duration, dict):
                lines.append(
                    "成片时长："
                    f"{int(duration.get('value', 0)) / max(int(duration.get('rate', 1)), 1):.3f}s。"
                )
            if render.get("captions"):
                lines.append(
                    f"字幕：{render.get('captions')} 条，"
                    f"{render.get('caption_srt_path')} / {render.get('caption_vtt_path')}。"
                )
        elif not ctx.allow_render:
            lines.append("本次按要求只做分析与规划，未执行渲染。")
        elif ctx.shared.revision is None:
            lines.append(
                "本次未产出成片：未生成 EditIR"
                "（规划不可用，或已关闭离线规则兜底且未提供手工 IR）。"
            )
        else:
            lines.append("本次未产出成片：规划或渲染未完成。")

        verify = ctx.shared.verify_result
        if verify is not None:
            lines.append(f"独立校验：{'通过' if verify.get('ok') else '发现差异'}。")

        if failed:
            lines.append("失败任务：" + "、".join(item.task_id for item in failed) + "。")
        if ctx.shared.warnings:
            preview = "；".join(ctx.shared.warnings[:5])
            suffix = " 等" if len(ctx.shared.warnings) > 5 else ""
            lines.append(f"注意：{preview}{suffix}")
        return "\n".join(lines)

    def _confidence(self, ctx: AgentContext, status: str, succeeded: int, total: int) -> float:
        if ctx.phase in {"outline", "fast_outline"}:
            return 0.7
        base: float
        verify = ctx.shared.verify_result
        if verify is not None and verify.get("ok"):
            base = 0.95
        elif ctx.shared.render_result is not None:
            base = 0.8
        elif ctx.shared.revision is not None:
            base = 0.6
        elif total > 0 and succeeded == total:
            base = 0.5
        else:
            base = 0.2
        review = ctx.shared.review
        if review is not None:
            base = min(base, max(0.2, float(review.get("score") or base)))
        return base

    def _llm_answer(
        self,
        ctx: AgentContext,
        ledger: list[TaskOutcome],
        status: str,
        *,
        cancel_token: Any | None = None,
    ) -> str:
        assert self.llm is not None
        payload = {
            "status": status,
            "goal": ctx.goal,
            "facts": ctx.shared.facts.model_dump(mode="json") if ctx.shared.facts else None,
            "transcript": ctx.shared.transcript_summary,
            "revision": ctx.shared.revision,
            "render": ctx.shared.render_result,
            "verify": ctx.shared.verify_result,
            "tasks": [
                {
                    "task_id": item.task_id,
                    "agent": item.agent,
                    "status": item.status,
                    "summary": item.summary,
                    "error": item.error,
                }
                for item in ledger
            ],
            "warnings": ctx.shared.warnings,
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是剪辑系统的最终决策智能体。根据结构化结果，用中文给出"
                    "简洁、准确、可执行的总结；不要编造未发生的结果。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ]
        return self.llm.chat_text(
            messages,
            cancel_token=cancel_token,
            usage_scope=ctx.run_id,
            token_budget=self.settings.llm_token_budget_per_run or None,
        ).strip()
