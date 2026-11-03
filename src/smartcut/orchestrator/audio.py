"""音频局部修复：按词级时间戳移除口癖词。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction

from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.time import RationalTime
from smartcut.contracts.transcript import Transcript

_NORMALIZE = re.compile(r"[^\w\u3400-\u4dbf\u4e00-\u9fff]+")


@dataclass
class AudioRepairReport:
    ir: EditIR
    changed: bool = False
    removed_words: int = 0
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def normalize_word(text: str) -> str:
    return _NORMALIZE.sub("", text or "").lower()


def _subtract(
    start: Fraction,
    end: Fraction,
    forbidden: list[tuple[Fraction, Fraction]],
    min_segment: Fraction,
) -> list[tuple[Fraction, Fraction]]:
    kept: list[tuple[Fraction, Fraction]] = []
    cursor = start
    for block_start, block_end in forbidden:
        if block_end <= start or block_start >= end:
            continue
        if block_start > cursor:
            kept.append((cursor, min(block_start, end)))
        cursor = max(cursor, block_end)
        if cursor >= end:
            break
    if cursor < end:
        kept.append((cursor, end))
    return [(a, b) for a, b in kept if b - a >= min_segment]


def repair_audio(
    ir: EditIR,
    transcript: Transcript | None,
    *,
    filler_words: set[str] | None = None,
    padding: float = 0.05,
    min_segment_seconds: float = 0.4,
) -> AudioRepairReport:
    if not ir.audio.remove_filler:
        return AudioRepairReport(ir=ir)
    fillers = {
        normalize_word(item)
        for item in (filler_words or set())
        if normalize_word(item)
    }
    words = transcript.words if transcript is not None else []
    if not words:
        return AudioRepairReport(
            ir=ir,
            warnings=["请求去口癖，但没有词级转写，已跳过"],
        )

    pad = Fraction(str(max(0.0, padding)))
    intervals: list[tuple[Fraction, Fraction]] = []
    removed = 0
    for word in words:
        if normalize_word(word.text) not in fillers:
            continue
        start = max(Fraction(0), word.start.as_fraction() - pad)
        end = word.end.as_fraction() + pad
        if end > start:
            intervals.append((start, end))
            removed += 1
    if not intervals:
        return AudioRepairReport(
            ir=ir,
            changes=["未发现配置中的口癖词"],
        )
    intervals.sort()
    merged: list[tuple[Fraction, Fraction]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    min_segment = Fraction(str(max(0.0, min_segment_seconds)))
    repaired: list[TrimOp] = []
    warnings: list[str] = []
    for op in ir.sequence:
        start = op.range.start.as_fraction()
        end = op.range.end.as_fraction()
        kept = _subtract(start, end, merged, min_segment)
        if not kept:
            warnings.append("片段完全由口癖组成，保留原片段")
            repaired.append(op)
            continue
        for piece_start, piece_end in kept:
            repaired.append(
                TrimOp(
                    # 区间未变的片段保留原 id；裁剪出的新片段由服务层分配 id。
                    id=(
                        op.id
                        if piece_start == start and piece_end == end
                        else None
                    ),
                    src=op.src,
                    range=TimeRange(
                        start=RationalTime.from_fraction(piece_start),
                        end=RationalTime.from_fraction(piece_end),
                    ),
                    speed=op.speed,
                    tag=op.tag,
                    confidence=op.confidence,
                )
            )

    new_ir = EditIR(
        project_id=ir.project_id,
        revision=ir.revision,
        media_facts=ir.media_facts,
        sequence=repaired,
        effects=ir.effects,
        transitions=ir.transitions,
        overlays=ir.overlays,
        removals=ir.removals,
        audio=ir.audio.model_copy(update={"remove_filler": False}),
        subtitles=ir.subtitles,
        export=ir.export,
    )
    return AudioRepairReport(
        ir=new_ir,
        changed=repaired != ir.sequence,
        removed_words=removed,
        changes=[f"移除 {removed} 个口癖词区间并合并片段"] if repaired != ir.sequence else [],
        warnings=warnings,
    )
