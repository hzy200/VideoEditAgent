"""DeepSeek 规划客户端：只产出编辑决策草稿，不产出路径或命令。"""

from __future__ import annotations

import re
from typing import Any

import httpx

from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR, TrimOp, assign_trim_ids
from smartcut.contracts.effects import Effect, validate_effects
from smartcut.contracts.loudness import parse_target_lufs
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.review import EditReview
from smartcut.contracts.time import RationalTime, sum_rational_times
from smartcut.contracts.transcript import Transcript
from smartcut.errors import (
    IRValidationError,
    LLMResponseError,
    PlannerResponseError,
    PlannerUnavailable,
    UnsupportedFeature,
)
from smartcut.llm.client import DeepSeekClient, TextCache
from smartcut.planner.context import (
    ChapterSelection,
    EditIRDraft,
    filter_transcript_to_chapters,
)
from smartcut.planner.prompts import (
    build_chapter_selection_messages,
    build_messages,
    build_replan_messages,
    build_review_messages,
)
from smartcut.resources import ResourceManager

MAX_REPAIR_ATTEMPTS = 2


def _to_trim_ops(draft: EditIRDraft) -> list[TrimOp]:
    """模型不提供 id；按源区间派生稳定 id，供 pinned 逐片段特效引用。"""
    return assign_trim_ids(
        [
            TrimOp(
                op=op.op,
                src=op.src,
                range=op.range,
                tag=op.tag,
                confidence=op.confidence,
                rationale=op.rationale,
            )
            for op in draft.sequence
        ]
    )


class DeepSeekPlanner:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        cache: TextCache | None = None,
        resources: ResourceManager | None = None,
    ) -> None:
        self.settings = settings
        self.llm = DeepSeekClient(
            settings,
            client=client,
            transport=transport,
            cache=cache,
            resources=resources,
        )

    @property
    def available(self) -> bool:
        return self.llm.available

    @property
    def llm_available(self) -> bool:
        return self.llm.available

    def close(self) -> None:
        self.llm.close()

    def usage_snapshot(self) -> dict[str, int]:
        return self.llm.usage_snapshot()

    def usage_for(self, scope: str | None) -> dict[str, int]:
        return self.llm.usage_for(scope)

    def propose(
        self,
        facts: MediaFacts,
        transcript: Transcript | None,
        goal: str,
        *,
        project_id: str,
        revision: int,
        perception: dict | None = None,
        chapters: dict | None = None,
        candidates: dict | None = None,
        visual_notes: dict | None = None,
        timeline: Any | None = None,
        memory: Any | None = None,
        preference: Any | None = None,
        history: list[dict] | None = None,
        chapter_ids: list[str] | None = None,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
        context_stats: dict | None = None,
        pinned_effects: list[Effect] | None = None,
    ) -> EditIR:
        if not self.available:
            raise PlannerUnavailable(
                "未配置 DEEPSEEK_API_KEY；可使用手工 IR 提交接口完成 P0 闭环"
            )

        selected_chapters = chapters
        prompt_transcript = transcript
        allowed_ranges = None
        chapter_items = (chapters or {}).get("chapters") or []
        if chapter_ids:
            by_id = {chapter.get("id"): chapter for chapter in chapter_items}
            missing = [item for item in chapter_ids if item not in by_id]
            if missing:
                raise PlannerResponseError(
                    f"人工确认章节不存在于当前 ChapterPlan: {missing}"
                )
            chosen = [by_id[item] for item in chapter_ids]
            selected_chapters = {
                "chapters": chosen,
                "chapter_count": len(chosen),
                "rationale": "人工确认大纲",
            }
            prompt_transcript = filter_transcript_to_chapters(
                transcript,
                selected_chapters,
            )
            allowed_ranges = self._chapter_ranges(selected_chapters)
        elif len(chapter_items) > 1:
            selection = self._select_chapters(
                goal,
                chapters,
                cancel_token=cancel_token,
                usage_scope=usage_scope,
                token_budget=token_budget,
            )
            if selection is not None:
                chosen_ids = set(selection.chapter_ids)
                chosen = [
                    chapter
                    for chapter in chapter_items
                    if chapter.get("id") in chosen_ids
                ]
                if chosen:
                    selected_chapters = {
                        "chapters": chosen,
                        "chapter_count": len(chosen),
                        "rationale": selection.rationale,
                    }
                    prompt_transcript = filter_transcript_to_chapters(
                        transcript,
                        selected_chapters,
                    )
                    allowed_ranges = self._chapter_ranges(selected_chapters)

        allowed_labels = (
            self._evidence_labels(visual_notes) if visual_notes else None
        )
        messages = build_messages(
            facts,
            prompt_transcript,
            goal,
            perception,
            selected_chapters,
            candidates,
            visual_notes,
            timeline=timeline,
            memory=memory,
            preference=preference,
            history=history,
            chapter_ids=chapter_ids,
            max_tokens=self.settings.planner_context_max_tokens,
            stats=context_stats,
            pinned_effects=pinned_effects,
        )
        try:
            draft = self.llm.chat_json(
                messages,
                EditIRDraft,
                validate=lambda item: self._check_draft(
                    item,
                    facts,
                    allowed_ranges,
                    check_effects=pinned_effects is None,
                    allowed_labels=allowed_labels,
                ),
                max_repairs=MAX_REPAIR_ATTEMPTS,
                cancel_token=cancel_token,
                usage_scope=usage_scope,
                token_budget=token_budget,
            )
        except LLMResponseError as exc:
            raise PlannerResponseError(str(exc)) from exc

        sequence = _to_trim_ops(draft)
        return EditIR(
            project_id=project_id,
            revision=revision,
            media_facts=facts,
            sequence=sequence,
            effects=self._effects_for_draft(draft, sequence, pinned_effects),
            audio=draft.audio,
            export=draft.export,
        )

    def replan(
        self,
        ir: EditIR,
        transcript: Transcript | None,
        goal: str,
        review: dict | None,
        *,
        project_id: str | None = None,
        revision: int | None = None,
        perception: dict | None = None,
        chapters: dict | None = None,
        candidates: dict | None = None,
        visual_notes: dict | None = None,
        memory: Any | None = None,
        preference: Any | None = None,
        history: list[dict] | None = None,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
        context_stats: dict | None = None,
        pinned_effects: list[Effect] | None = None,
    ) -> EditIR:
        """根据 QA 问题重规划；输出仍是完整 EditIR，由服务层存为新 revision。"""
        if not self.available:
            raise PlannerUnavailable("未配置 DEEPSEEK_API_KEY")
        messages = build_replan_messages(
            ir,
            transcript,
            goal,
            review,
            perception=perception,
            chapters=chapters,
            candidates=candidates,
            visual_notes=visual_notes,
            memory=memory,
            preference=preference,
            history=history,
            max_tokens=self.settings.planner_context_max_tokens,
            stats=context_stats,
            pinned_effects=pinned_effects,
        )
        allowed_labels = (
            self._evidence_labels(visual_notes) if visual_notes else None
        )
        try:
            draft = self.llm.chat_json(
                messages,
                EditIRDraft,
                validate=lambda item: self._check_draft(
                    item,
                    ir.media_facts,
                    None,
                    check_effects=pinned_effects is None,
                    allowed_labels=allowed_labels,
                ),
                max_repairs=MAX_REPAIR_ATTEMPTS,
                temperature=0.15,
                cancel_token=cancel_token,
                usage_scope=usage_scope,
                token_budget=token_budget,
            )
        except LLMResponseError as exc:
            raise PlannerResponseError(str(exc)) from exc
        sequence = _to_trim_ops(draft)
        return EditIR(
            project_id=project_id or ir.project_id,
            revision=revision if revision is not None else ir.revision,
            media_facts=ir.media_facts,
            sequence=sequence,
            effects=self._effects_for_draft(draft, sequence, pinned_effects),
            audio=draft.audio,
            export=draft.export,
        )

    def review(
        self,
        ir: EditIR,
        transcript: Transcript | None,
        goal: str,
        *,
        perception: dict | None = None,
        visual_notes: dict | None = None,
        timeline: Any | None = None,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
    ) -> EditReview:
        if not self.available:
            raise PlannerUnavailable("未配置 DEEPSEEK_API_KEY")
        messages = build_review_messages(
            ir,
            transcript,
            goal,
            perception,
            visual_notes,
            timeline=timeline,
        )
        try:
            return self.llm.chat_json(
                messages,
                EditReview,
                max_repairs=1,
                temperature=0.1,
                cancel_token=cancel_token,
                usage_scope=usage_scope,
                token_budget=token_budget,
            )
        except LLMResponseError as exc:
            raise PlannerResponseError(str(exc)) from exc

    def _select_chapters(
        self,
        goal: str,
        chapters: dict | None,
        *,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
    ) -> ChapterSelection | None:
        if not chapters:
            return None
        try:
            return self.llm.chat_json(
                build_chapter_selection_messages(goal, chapters),
                ChapterSelection,
                max_repairs=1,
                cancel_token=cancel_token,
                usage_scope=usage_scope,
                token_budget=token_budget,
            )
        except LLMResponseError:
            return None

    @staticmethod
    def _fully_inside(start, end, ranges: list[tuple]) -> bool:
        """合并相邻/重叠的已选章节区间，片段必须完整落在其中一个区间内。"""
        ordered = sorted(ranges)
        merged: list[list] = []
        for range_start, range_end in ordered:
            if not merged or range_start > merged[-1][1]:
                merged.append([range_start, range_end])
            else:
                merged[-1][1] = max(merged[-1][1], range_end)
        return any(
            start >= range_start and end <= range_end
            for range_start, range_end in merged
        )

    @staticmethod
    def _chapter_ranges(chapters: dict | None) -> list[tuple]:
        ranges: list[tuple] = []
        for chapter in (chapters or {}).get("chapters") or []:
            try:
                start = RationalTime.model_validate(chapter["start"]).as_fraction()
                end = RationalTime.model_validate(chapter["end"]).as_fraction()
            except (KeyError, ValueError):
                continue
            ranges.append((start, end))
        return ranges

    @staticmethod
    def _effects_for_draft(
        draft: EditIRDraft,
        sequence: list[TrimOp],
        pinned_effects: list[Effect] | None,
    ) -> list:
        if pinned_effects is None:
            return list(draft.effects)
        total_duration = sum_rational_times(
            [op.range.duration() for op in sequence]
        )
        segment_durations = {
            op.id: op.range.duration() for op in sequence if op.id
        }
        try:
            validate_effects(
                pinned_effects,
                total_duration=total_duration,
                segment_durations=segment_durations,
            )
        except (IRValidationError, UnsupportedFeature) as exc:
            raise PlannerResponseError(
                f"锁定特效与 AI 时间线不兼容: {exc}"
            ) from exc
        return list(pinned_effects)

    @staticmethod
    def _evidence_labels(visual_notes: Any) -> set[str]:
        if visual_notes is None:
            return set()
        payload = (
            visual_notes
            if isinstance(visual_notes, dict)
            else visual_notes.model_dump(mode="json")
        )
        labels: set[str] = set()
        for chapter in payload.get("chapters") or []:
            labels.update(
                str(ref) for ref in chapter.get("evidence_refs") or []
            )
        return labels

    @staticmethod
    def _check_evidence_refs(
        draft: EditIRDraft,
        allowed_labels: set[str],
    ) -> None:
        for index, op in enumerate(draft.sequence):
            used = set(re.findall(r"T\d{2,3}", op.rationale or ""))
            unknown = used - allowed_labels
            if unknown:
                raise ValueError(
                    f"sequence[{index}].rationale 引用了不存在的证据标签: "
                    f"{sorted(unknown)}"
                )

    def _check_draft(
        self,
        draft: EditIRDraft,
        facts: MediaFacts,
        allowed_ranges: list[tuple] | None = None,
        *,
        check_effects: bool = True,
        allowed_labels: set[str] | None = None,
    ) -> None:
        if allowed_labels is not None:
            self._check_evidence_refs(draft, allowed_labels)
        for index, op in enumerate(draft.sequence):
            start = op.range.start.as_fraction()
            end = op.range.end.as_fraction()
            if op.src != facts.src:
                raise ValueError(
                    f"sequence[{index}].src 必须等于 {facts.src!r}，实际为 {op.src!r}"
                )
            if start < 0:
                raise ValueError(f"sequence[{index}] 起点为负")
            if op.range.end > facts.duration:
                raise ValueError(
                    f"sequence[{index}] 终点越界: {op.range.end.to_ffmpeg_seconds()}s "
                    f"> {facts.duration.to_ffmpeg_seconds()}s"
                )
            if allowed_ranges and not self._fully_inside(
                start, end, allowed_ranges
            ):
                raise ValueError(
                    f"sequence[{index}] 未完整落在已选章节范围内: [{start}, {end}]"
                )

        if check_effects:
            if any(effect.target == "segment" for effect in draft.effects):
                raise ValueError(
                    "LLM 不能直接输出逐片段特效；请通过 GUI 或 pinned_effects 设置"
                )
            total_duration = sum_rational_times(
                [op.range.duration() for op in draft.sequence]
            )
            try:
                validate_effects(draft.effects, total_duration=total_duration)
            except (IRValidationError, UnsupportedFeature) as exc:
                raise ValueError(str(exc)) from exc
        if draft.audio.bgm is not None:
            raise ValueError("P1 不支持 bgm")
        if draft.audio.loudness is not None:
            parse_target_lufs(draft.audio.loudness)
        if draft.export.preset != "mp4_h264":
            raise ValueError("P1 只允许 preset=mp4_h264")
