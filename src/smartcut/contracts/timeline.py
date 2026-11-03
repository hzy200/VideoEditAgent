"""时间线记忆：已用源区间的结构化摘要。

分工：
- 只做结构派生，不改变 ``EditIR``，不产生工具调用；
- 输出时间由 sequence 顺序推导，与 ``compiler`` 的口径一致（90000 时基）；
- 供 Planner/Critic/QA/Decision 读取"已覆盖、未覆盖、重复"事实，
  避免模型自己做时间算术，也避免每轮从零重算时间线。

所有区间运算走 ``Fraction``；比例字段只是展示用浮点数。
"""

from __future__ import annotations

from fractions import Fraction

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.edit_ir import EditIR, TimeRange
from smartcut.contracts.time import RationalTime

_RATE = 90_000


def _as_rational(value: Fraction) -> RationalTime:
    return RationalTime.from_fraction(value).round_to_rate(_RATE)


def _merge(intervals: list[tuple[Fraction, Fraction]]) -> list[tuple[Fraction, Fraction]]:
    ordered = sorted(intervals)
    merged: list[list[Fraction]] = []
    for start, end in ordered:
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


class UsedRange(BaseModel):
    """一次 trim 的源区间与它在输出时间线上的位置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    src_start: RationalTime
    src_end: RationalTime
    out_start: RationalTime
    out_end: RationalTime
    tag: str | None = None
    origin: str = "plan"


class TimelineMemory(BaseModel):
    """某次 revision 的时间线账本：哪些源区间被用过、覆盖多少、重复多少。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    src: str
    source_hash: str | None = None
    revision: int = 0
    used: list[UsedRange] = Field(default_factory=list)
    coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    duplicate_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    output_duration: RationalTime
    unused_gaps: list[TimeRange] = Field(default_factory=list)


def build_timeline_memory(
    ir: EditIR,
    *,
    origin: str = "plan",
    source_hash: str | None = None,
) -> TimelineMemory:
    """从 EditIR 派生时间线账本；纯函数，不做隐式修正。"""
    duration = max(Fraction(0), ir.media_facts.duration.as_fraction())
    cursor = Fraction(0)
    used_total = Fraction(0)
    used: list[UsedRange] = []
    intervals: list[tuple[Fraction, Fraction]] = []

    for op in ir.sequence:
        start = max(Fraction(0), op.range.start.as_fraction())
        end = op.range.end.as_fraction()
        if duration > 0:
            end = min(end, duration)
        if end <= start:
            continue
        length = end - start
        used.append(
            UsedRange(
                src_start=_as_rational(start),
                src_end=_as_rational(end),
                out_start=_as_rational(cursor),
                out_end=_as_rational(cursor + length),
                tag=op.tag,
                origin=origin,
            )
        )
        cursor += length
        used_total += length
        intervals.append((start, end))

    merged = _merge(intervals)
    covered = sum((end - start for start, end in merged), Fraction(0))

    gaps: list[TimeRange] = []
    position = Fraction(0)
    for start, end in merged:
        if start > position:
            gaps.append(TimeRange(start=_as_rational(position), end=_as_rational(start)))
        position = max(position, end)
    if duration > 0 and position < duration:
        gaps.append(TimeRange(start=_as_rational(position), end=_as_rational(duration)))

    return TimelineMemory(
        src=ir.media_facts.src,
        source_hash=source_hash,
        revision=ir.revision,
        used=used,
        coverage_ratio=float(covered / duration) if duration > 0 else 0.0,
        duplicate_ratio=float((used_total - covered) / used_total)
        if used_total > 0
        else 0.0,
        output_duration=_as_rational(cursor),
        unused_gaps=gaps,
    )
