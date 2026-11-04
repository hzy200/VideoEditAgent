"""离线规则规划器：无 LLM 时产出确定性的 EditIR。

只做“可判定”的选择：
- 从 goal 解析目标时长（``60 秒``、``2 分钟``、``前 30 秒``）；
- 候选片段按 score 排序，剔除质量黑/糊/重复区间；
- 缺口回退到章节 / 转写 / 全片；
- 超长片段按 ``critic_max_segment_seconds`` 拆分；
- 所有时间运算走 ``Fraction``/``RationalTime``，不拼路径或命令。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Any

from smartcut.config import Settings
from smartcut.contracts.edit_ir import (
    EditIR,
    TimeRange,
    TrimOp,
    assign_trim_ids,
)
from smartcut.contracts.effects import Effect, validate_effects
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.review import EditReview, ReviewIssue
from smartcut.contracts.time import RationalTime, sum_rational_times
from smartcut.errors import (
    IRValidationError,
    PlannerResponseError,
    UnsupportedFeature,
)

QUALITY_FLAGS = frozenset({"black", "frozen", "blurry", "duplicate"})
MIN_INTERVAL = Fraction(1, 10)
SNAP_TOLERANCE = Fraction(1, 2)

_UNIT_SECONDS = {
    "小时": 3600,
    "hour": 3600,
    "hours": 3600,
    "h": 3600,
    "分钟": 60,
    "分": 60,
    "minute": 60,
    "minutes": 60,
    "mins": 60,
    "min": 60,
    "m": 60,
    "秒": 1,
    "seconds": 1,
    "second": 1,
    "secs": 1,
    "sec": 1,
    "s": 1,
}
_DURATION_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(小时|分钟|分|秒|hours|hour|minutes|minute|mins|min|secs|seconds|sec|h|s|m)",
    re.IGNORECASE,
)
_PREFIX_RE = re.compile(
    r"前\s*(\d+(?:\.\d+)?)\s*(秒|seconds|second|secs|sec|s)",
    re.IGNORECASE,
)


@dataclass
class _Interval:
    start: Fraction
    end: Fraction
    score: float = 0.5
    tag: str | None = "rule"
    rationale: str = "规则选择"

    def duration(self) -> Fraction:
        return self.end - self.start


def _to_fraction(value: Any) -> Fraction | None:
    if value is None:
        return None
    if isinstance(value, RationalTime):
        return value.as_fraction()
    try:
        return RationalTime.model_validate(value).as_fraction()
    except (TypeError, ValueError):
        return None


def parse_target_seconds(goal: str, duration: Fraction) -> tuple[Fraction, bool]:
    """解析目标时长；返回 ``(target, prefer_start)``，无法解析时保留全片。"""
    text = (goal or "").strip()
    prefix = _PREFIX_RE.search(text)
    if prefix:
        target = Fraction(prefix.group(1))
        return min(max(target, MIN_INTERVAL), duration), True
    total = Fraction(0)
    for number, unit in _DURATION_RE.findall(text):
        multiplier = _UNIT_SECONDS.get(unit.lower(), 1)
        total += Fraction(number) * multiplier
    if total <= 0:
        return duration, False
    return min(total, duration), False


def _fully_inside(start: Fraction, end: Fraction, ranges: list[tuple[Fraction, Fraction]]) -> bool:
    return any(start >= left and end <= right for left, right in ranges)


def _chapter_ranges(chapters: dict | None) -> list[tuple[Fraction, Fraction]]:
    ranges: list[tuple[Fraction, Fraction]] = []
    for chapter in (chapters or {}).get("chapters") or []:
        start = _to_fraction(chapter.get("start"))
        end = _to_fraction(chapter.get("end"))
        if start is None or end is None or end <= start:
            continue
        ranges.append((start, end))
    return ranges


def _quality_times(perception: dict | None) -> list[Fraction]:
    marks = ((perception or {}).get("quality") or {}).get("marks") or []
    times: list[Fraction] = []
    for mark in marks:
        if mark.get("kind") not in QUALITY_FLAGS:
            continue
        moment = _to_fraction(mark.get("t"))
        if moment is not None:
            times.append(moment)
    return times


def _shot_times(perception: dict | None) -> list[Fraction]:
    shots = ((perception or {}).get("shots") or {}).get("shots") or []
    times: list[Fraction] = []
    for shot in shots:
        moment = _to_fraction(shot.get("t"))
        if moment is not None:
            times.append(moment)
    return times


def _snap(
    value: Fraction,
    boundaries: list[Fraction],
    tolerance: Fraction = SNAP_TOLERANCE,
) -> Fraction:
    best = value
    best_distance: Fraction | None = None
    for boundary in boundaries:
        distance = abs(boundary - value)
        if distance > tolerance:
            continue
        if best_distance is None or distance < best_distance:
            best = boundary
            best_distance = distance
    return best


def _subtract(
    interval: _Interval,
    others: list[_Interval],
) -> list[_Interval]:
    pieces = [(interval.start, interval.end)]
    for other in others:
        next_pieces: list[tuple[Fraction, Fraction]] = []
        for start, end in pieces:
            if other.end <= start or other.start >= end:
                next_pieces.append((start, end))
                continue
            if start < other.start:
                next_pieces.append((start, other.start))
            if other.end < end:
                next_pieces.append((other.end, end))
        pieces = next_pieces
    return [
        replace(interval, start=start, end=end)
        for start, end in pieces
        if end - start >= MIN_INTERVAL
    ]


def _subtract_windows(
    intervals: list[_Interval],
    moments: list[Fraction],
    padding: Fraction,
) -> list[_Interval]:
    if not moments:
        return intervals
    windows = [
        _Interval(start=max(0, moment - padding), end=moment + padding)
        for moment in moments
    ]
    result: list[_Interval] = []
    for interval in intervals:
        result.extend(_subtract(interval, windows))
    return result


class RulePlanner:
    """与 DeepSeekPlanner 同协议的确定性规划器；``available`` 恒为 True。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def available(self) -> bool:
        return True

    @property
    def llm_available(self) -> bool:
        return False

    def close(self) -> None:
        return None

    def usage_snapshot(self) -> dict[str, int]:
        return {}

    def usage_for(self, scope: str | None) -> dict[str, int]:
        return {}

    def propose(
        self,
        facts: MediaFacts,
        transcript: Any | None,
        goal: str,
        *,
        project_id: str,
        revision: int,
        **kwargs: Any,
    ) -> EditIR:
        duration = facts.duration.as_fraction()
        if duration <= 0:
            raise PlannerResponseError("素材时长为 0，无法规划")
        perception = kwargs.get("perception")
        chapters = kwargs.get("chapters")
        candidates = kwargs.get("candidates")
        chapter_ids = kwargs.get("chapter_ids")
        pinned_effects = kwargs.get("pinned_effects")

        chapter_ranges = _chapter_ranges(chapters)
        if chapter_ids:
            wanted = set(chapter_ids)
            chapter_ranges = [
                (_to_fraction(chapter.get("start")), _to_fraction(chapter.get("end")))
                for chapter in (chapters or {}).get("chapters") or []
                if chapter.get("id") in wanted
            ]
            chapter_ranges = [
                (start, end)
                for start, end in chapter_ranges
                if start is not None and end is not None and end > start
            ]

        target, prefer_start = parse_target_seconds(goal, duration)
        quality = _quality_times(perception)
        intervals = self._candidate_intervals(
            candidates,
            quality,
            chapter_ranges,
        )
        if intervals:
            selected = self._select_by_score(intervals, target, duration)
        else:
            selected = self._fallback_intervals(
                transcript,
                chapters,
                chapter_ranges,
                perception,
                quality,
                target,
                duration,
                prefer_start,
            )
        if not selected:
            selected = [_Interval(Fraction(0), duration, 0.5, "full", "规则回退：全片")]
        selected = self._split_long(selected)
        selected.sort(key=lambda item: (item.start, item.end))

        sequence = assign_trim_ids(
            [
                TrimOp(
                    src=facts.src,
                    range=TimeRange(
                        start=RationalTime.from_fraction(item.start),
                        end=RationalTime.from_fraction(min(item.end, duration)),
                    ),
                    tag=item.tag,
                    confidence=round(max(0.0, min(1.0, item.score)), 4),
                    rationale=item.rationale[:120],
                )
                for item in selected
                if min(item.end, duration) > item.start
            ]
        )
        if not sequence:
            raise PlannerResponseError("规则规划未产出有效片段")
        return EditIR(
            project_id=project_id,
            revision=revision,
            media_facts=facts,
            sequence=sequence,
            effects=self._effects(pinned_effects, sequence),
        )

    def replan(
        self,
        ir: EditIR,
        transcript: Any | None,
        goal: str,
        review: dict | None,
        *,
        project_id: str | None = None,
        revision: int | None = None,
        **kwargs: Any,
    ) -> EditIR:
        """离线模式不重写时间线：返回原 IR，由服务层判定为 unchanged。"""
        return ir.model_copy(
            update={
                "project_id": project_id or ir.project_id,
                "revision": revision if revision is not None else ir.revision,
            }
        )

    def review(
        self,
        ir: EditIR,
        transcript: Any | None,
        goal: str,
        **kwargs: Any,
    ) -> EditReview:
        issues: list[ReviewIssue] = []
        duration = ir.total_duration().to_float()
        if duration < 1.0:
            issues.append(
                ReviewIssue(
                    severity="warning",
                    message=f"成片过短（{duration:.2f}s），可能不符合目标",
                )
            )
        for index, op in enumerate(ir.sequence):
            if op.range.duration().to_float() < 0.5:
                issues.append(
                    ReviewIssue(
                        severity="info",
                        message="片段短于 0.5s，可能产生闪烁",
                        segment_index=index,
                        suggestion="考虑与相邻片段合并或删除",
                    )
                )
        warnings = sum(1 for item in issues if item.severity == "warning")
        errors = sum(1 for item in issues if item.severity == "error")
        return EditReview(
            score=max(0.2, 0.9 - 0.1 * warnings - 0.3 * errors),
            summary=(
                "规则审片通过"
                if not issues
                else f"发现 {len(issues)} 个问题，建议人工复核"
            ),
            issues=issues,
        )

    # ── 内部辅助 ────────────────────────────────────────────────────
    @staticmethod
    def _candidate_intervals(
        candidates: dict | None,
        quality: list[Fraction],
        chapter_ranges: list[tuple[Fraction, Fraction]],
    ) -> list[_Interval]:
        intervals: list[_Interval] = []
        for candidate in (candidates or {}).get("candidates") or []:
            source_range = candidate.get("source_range") or {}
            start = _to_fraction(source_range.get("start"))
            end = _to_fraction(source_range.get("end"))
            if start is None or end is None or end <= start:
                continue
            flags = set(candidate.get("quality_flags") or [])
            if flags & QUALITY_FLAGS:
                continue
            if any(start <= moment < end for moment in quality):
                continue
            if chapter_ranges and not _fully_inside(start, end, chapter_ranges):
                continue
            score = max(0.0, min(1.0, float(candidate.get("score") or 0.0)))
            tags = [
                str(tag).strip()
                for tag in candidate.get("tags") or []
                if str(tag).strip()
            ]
            tag = tags[0] if tags else "candidate"
            intervals.append(
                _Interval(
                    start,
                    end,
                    score,
                    tag,
                    f"规则候选：{tag}，得分 {score:.2f}",
                )
            )
        return intervals

    @staticmethod
    def _select_by_score(
        intervals: list[_Interval],
        target: Fraction,
        duration: Fraction,
    ) -> list[_Interval]:
        ranked = sorted(intervals, key=lambda item: (-item.score, item.start))
        chosen: list[_Interval] = []
        covered = Fraction(0)
        remaining = target
        for item in ranked:
            if remaining <= 0:
                break
            pieces = _subtract(item, chosen)
            if not pieces:
                continue
            # 同一候选与多个已选区间相减时，优先保留最长的剩余片段。
            piece = max(pieces, key=lambda value: value.duration())
            if piece.duration() > remaining:
                piece = replace(piece, end=piece.start + remaining)
            chosen.append(piece)
            covered += piece.duration()
            remaining -= piece.duration()
        if not chosen:
            return []
        # 输出按源时间顺序；若目标接近全片则尽量覆盖所有候选。
        if target >= duration:
            chosen = []
            for item in sorted(intervals, key=lambda value: (value.start, value.end)):
                promised = _subtract(item, chosen)
                if promised:
                    chosen.extend(promised)
        return chosen

    def _fallback_intervals(
        self,
        transcript: Any | None,
        chapters: dict | None,
        chapter_ranges: list[tuple[Fraction, Fraction]],
        perception: dict | None,
        quality: list[Fraction],
        target: Fraction,
        duration: Fraction,
        prefer_start: bool,
    ) -> list[_Interval]:
        padding = Fraction(str(self.settings.critic_quality_padding))
        boundaries = _shot_times(perception)
        candidates: list[_Interval] = []
        for chapter in (chapters or {}).get("chapters") or []:
            start = _to_fraction(chapter.get("start"))
            end = _to_fraction(chapter.get("end"))
            if start is None or end is None or end <= start:
                continue
            if chapter_ranges and not _fully_inside(start, end, chapter_ranges):
                continue
            title = str(chapter.get("title") or "章节")
            candidates.append(
                _Interval(start, end, 0.55, "chapter", f"规则章节：{title}")
            )

        if not candidates and transcript is not None:
            max_seconds = Fraction(str(self.settings.candidate_target_seconds))
            current_start: Fraction | None = None
            current_end: Fraction | None = None
            for segment in getattr(transcript, "segments", []) or []:
                start = segment.start.as_fraction()
                end = segment.end.as_fraction()
                if end <= start:
                    continue
                if current_start is None:
                    current_start, current_end = start, end
                    continue
                if end - current_start > max_seconds:
                    candidates.append(
                        _Interval(
                            current_start,
                            current_end or current_start,
                            0.45,
                            "transcript",
                            "规则转写：连续语句",
                        )
                    )
                    current_start, current_end = start, end
                else:
                    current_end = end
            if current_start is not None and current_end is not None:
                candidates.append(
                    _Interval(
                        current_start,
                        current_end,
                        0.45,
                        "transcript",
                        "规则转写：连续语句",
                    )
                )

        if not candidates:
            candidates = [
                _Interval(Fraction(0), duration, 0.5, "full", "规则回退：全片")
            ]

        snapped = [
            replace(
                item,
                start=_snap(item.start, boundaries),
                end=_snap(item.end, boundaries),
            )
            for item in candidates
            if item.end > item.start
        ]
        snapped = _subtract_windows(snapped, quality, padding)

        if target >= duration:
            return snapped
        selected: list[_Interval] = []
        remaining = target
        ordered = sorted(snapped, key=lambda item: item.start)
        if not prefer_start:
            # 无“前 N 秒”语义时优先高置信度章节，再按时间输出。
            ordered = sorted(snapped, key=lambda item: (-item.score, item.start))
        for item in ordered:
            if remaining <= 0:
                break
            take = min(item.duration(), remaining)
            if take < MIN_INTERVAL:
                continue
            if take < item.duration():
                rationale = (
                    f"规则前缀：前 {float(take):.1f}s"
                    if prefer_start
                    else f"{item.rationale}（按目标截取）"
                )
                selected.append(
                    replace(item, end=item.start + take, rationale=rationale[:120])
                )
            else:
                selected.append(item)
            remaining -= take
        selected.sort(key=lambda item: item.start)
        return selected

    def _split_long(self, intervals: list[_Interval]) -> list[_Interval]:
        limit = Fraction(str(self.settings.critic_max_segment_seconds))
        if limit <= 0:
            return intervals
        result: list[_Interval] = []
        for item in intervals:
            duration = item.duration()
            if duration <= limit:
                result.append(item)
                continue
            count = max(1, math.ceil(duration / limit))
            step = duration / count
            for index in range(count):
                start = item.start + step * index
                end = item.end if index == count - 1 else item.start + step * (index + 1)
                result.append(
                    replace(
                        item,
                        start=start,
                        end=end,
                        rationale=f"{item.rationale}（拆分 {index + 1}/{count}）"[:120],
                    )
                )
        return result

    @staticmethod
    def _effects(
        pinned_effects: list[Effect] | None,
        sequence: list[TrimOp],
    ) -> list[Effect]:
        if pinned_effects is None:
            return []
        total = sum_rational_times([op.output_duration() for op in sequence])
        segment_durations = {
            op.id: op.output_duration() for op in sequence if op.id
        }
        try:
            validate_effects(
                pinned_effects,
                total_duration=total,
                segment_durations=segment_durations,
            )
        except (IRValidationError, UnsupportedFeature) as exc:
            raise PlannerResponseError(f"锁定特效与规则时间线不兼容: {exc}") from exc
        return list(pinned_effects)
