"""Critic 局部重规划：基于质量标记与镜头结构修正规划片段。

纯函数，不访问文件系统；只调整 sequence，不改 effects/audio/export。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.quality import QualityKind, QualityReport
from smartcut.contracts.shots import ShotList
from smartcut.contracts.time import RationalTime

_BAD_KINDS = {QualityKind.black, QualityKind.blurry, QualityKind.duplicate}
_EPSILON = Fraction(1, 1000)


@dataclass
class CriticReport:
    ir: EditIR
    changed: bool = False
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _merge(intervals: list[tuple[Fraction, Fraction]]) -> list[tuple[Fraction, Fraction]]:
    ordered = sorted(intervals)
    merged: list[tuple[Fraction, Fraction]] = []
    for start, end in ordered:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _forbidden(
    quality: QualityReport | None,
    padding: float,
) -> list[tuple[Fraction, Fraction]]:
    if quality is None:
        return []
    sample = 1.0 / quality.sampled_fps if quality.sampled_fps > 0 else 1.0
    pad = Fraction(str(max(0.0, padding)))
    width = Fraction(str(max(sample, 0.001)))
    intervals = []
    for mark in quality.marks:
        if mark.kind not in _BAD_KINDS:
            continue
        t = mark.t.as_fraction()
        intervals.append((max(Fraction(0), t - pad), t + width + pad))
    return _merge(intervals)


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


def _split_by_shots(
    start: Fraction,
    end: Fraction,
    shot_points: list[Fraction],
    max_segment: Fraction,
    min_segment: Fraction,
) -> list[tuple[Fraction, Fraction]]:
    if max_segment <= 0 or end - start <= max_segment:
        return [(start, end)]
    pieces: list[tuple[Fraction, Fraction]] = []
    cursor = start
    while end - cursor > max_segment:
        upper = cursor + max_segment
        inside = [point for point in shot_points if cursor + min_segment <= point <= upper]
        cut = inside[-1] if inside else upper
        if cut <= cursor:
            cut = upper
        pieces.append((cursor, cut))
        cursor = cut
    if end - cursor >= min_segment:
        pieces.append((cursor, end))
    return pieces


def repair_sequence(
    ir: EditIR,
    quality: QualityReport | None = None,
    shots: ShotList | None = None,
    *,
    max_segment_seconds: float = 90.0,
    padding: float = 0.5,
    min_segment_seconds: float = 0.5,
) -> CriticReport:
    forbidden = _forbidden(quality, padding)
    shot_points = sorted(
        shot.t.as_fraction()
        for shot in (shots.shots if shots else [])
        if shot.t.as_fraction() > 0
    )
    max_segment = Fraction(str(max(0.0, max_segment_seconds)))
    min_segment = Fraction(str(max(0.0, min_segment_seconds)))

    repaired: list[TrimOp] = []
    changes: list[str] = []
    warnings: list[str] = []

    for op in ir.sequence:
        start = op.range.start.as_fraction()
        end = op.range.end.as_fraction()
        kept = _subtract(start, end, forbidden, min_segment)
        if not kept:
            warnings.append(
                f"片段 {op.range.start.to_ffmpeg_seconds()}-"
                f"{op.range.end.to_ffmpeg_seconds()}s 完全被质量标记覆盖，保留原片段"
            )
            repaired.append(op)
            continue
        removed = (end - start) - sum((b - a for a, b in kept), Fraction(0))
        if removed > 0:
            changes.append(
                f"移除 {float(removed):.2f}s 黑帧/模糊/重复帧区间"
            )
        for piece_start, piece_end in kept:
            for split_start, split_end in _split_by_shots(
                piece_start,
                piece_end,
                shot_points,
                max_segment,
                min_segment,
            ):
                repaired.append(
                    TrimOp(
                        # 区间未变的片段保留原 id；拆分出的新片段由服务层分配 id。
                        id=(
                            op.id
                            if split_start == start and split_end == end
                            else None
                        ),
                        src=op.src,
                        range=TimeRange(
                            start=RationalTime.from_fraction(split_start),
                            end=RationalTime.from_fraction(split_end),
                        ),
                        speed=op.speed,
                        tag=op.tag,
                        confidence=op.confidence,
                    )
                )
                if (split_end - split_start) < (piece_end - piece_start):
                    changes.append(
                        f"按镜头/时长拆分长片段 {float(piece_end - piece_start):.1f}s"
                    )

    if not repaired:
        warnings.append("Critic 未得到可用片段，保留原方案")
        return CriticReport(ir=ir, warnings=warnings)

    if repaired == ir.sequence:
        return CriticReport(ir=ir, warnings=warnings)

    new_ir = EditIR(
        project_id=ir.project_id,
        revision=ir.revision,
        media_facts=ir.media_facts,
        sequence=repaired,
        effects=ir.effects,
        transitions=ir.transitions,
        overlays=ir.overlays,
        removals=ir.removals,
        audio=ir.audio,
        subtitles=ir.subtitles,
        export=ir.export,
    )
    # 同一原因只记录一次，避免长视频里重复刷屏。
    deduped: list[str] = []
    for item in changes:
        if item not in deduped:
            deduped.append(item)
    return CriticReport(
        ir=new_ir,
        changed=True,
        changes=deduped,
        warnings=warnings,
    )
