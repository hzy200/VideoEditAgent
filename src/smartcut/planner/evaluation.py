"""选段质量评估：对比人工 EDL 与规划时间线的区间 IoU。

指标只基于源时间区间，不依赖模型文本，便于 CI 对规则规划设置下限。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

Interval = tuple[float, float]


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    ordered = sorted(
        (max(0.0, float(start)), max(0.0, float(end)))
        for start, end in intervals
        if float(end) > float(start)
    )
    merged: list[Interval] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def total_length(intervals: Iterable[Interval]) -> float:
    return sum(end - start for start, end in intervals)


def intersection_length(left: Iterable[Interval], right: Iterable[Interval]) -> float:
    a = merge_intervals(left)
    b = merge_intervals(right)
    i = j = 0
    total = 0.0
    while i < len(a) and j < len(b):
        start = max(a[i][0], b[j][0])
        end = min(a[i][1], b[j][1])
        if end > start:
            total += end - start
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return total


def mean_best_iou(
    planned: Iterable[Interval],
    expected: Iterable[Interval],
) -> float:
    planned_items = list(planned)
    expected_items = list(expected)
    if not planned_items or not expected_items:
        return 0.0
    scores: list[float] = []
    for start, end in planned_items:
        best = 0.0
        duration = end - start
        for other_start, other_end in expected_items:
            overlap = max(0.0, min(end, other_end) - max(start, other_start))
            if overlap <= 0:
                continue
            union = duration + (other_end - other_start) - overlap
            if union > 0:
                best = max(best, overlap / union)
        scores.append(best)
    return sum(scores) / len(scores)


def _f1(precision: float, recall: float) -> float:
    if precision + recall <= 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def evaluate_selection(
    planned: Iterable[Interval],
    expected: Iterable[Interval],
    *,
    source_duration: float,
) -> dict[str, Any]:
    planned_items = [(float(start), float(end)) for start, end in planned if end > start]
    expected_items = [(float(start), float(end)) for start, end in expected if end > start]
    planned_union = merge_intervals(planned_items)
    expected_union = merge_intervals(expected_items)
    planned_total = total_length(planned_union)
    expected_total = total_length(expected_union)
    overlap = intersection_length(planned_union, expected_union)

    precision = overlap / planned_total if planned_total > 0 else 0.0
    recall = overlap / expected_total if expected_total > 0 else 0.0
    f1 = _f1(precision, recall)

    segment_precision = mean_best_iou(planned_items, expected_items)
    segment_recall = mean_best_iou(expected_items, planned_items)
    segment_f1 = _f1(segment_precision, segment_recall)

    raw_planned = total_length(planned_items)
    duplicate_ratio = (
        max(0.0, (raw_planned - planned_total) / raw_planned)
        if raw_planned > 0
        else 0.0
    )
    coverage_ratio = (
        min(1.0, planned_total / source_duration)
        if source_duration > 0
        else 0.0
    )
    duration_deviation = (
        abs(planned_total - expected_total) / expected_total
        if expected_total > 0
        else 0.0
    )
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "segment_precision": round(segment_precision, 4),
        "segment_recall": round(segment_recall, 4),
        "segment_f1": round(segment_f1, 4),
        "coverage_ratio": round(coverage_ratio, 4),
        "duplicate_ratio": round(duplicate_ratio, 4),
        "duration_deviation": round(duration_deviation, 4),
        "planned_seconds": round(planned_total, 3),
        "expected_seconds": round(expected_total, 3),
        "segment_count": len(planned_items),
        "expected_count": len(expected_items),
    }


def ir_intervals(ir: Any) -> list[Interval]:
    """从 EditIR 或 ``dump_ir()`` 字典提取源区间（秒）。"""
    if hasattr(ir, "sequence"):
        return [
            (
                op.range.start.to_float(),
                op.range.end.to_float(),
            )
            for op in ir.sequence
        ]
    payload = dict(ir or {})
    intervals: list[Interval] = []
    for item in payload.get("sequence") or []:
        time_range = item.get("range") or {}
        try:
            start = float(time_range["start"]["value"]) / float(time_range["start"]["rate"])
            end = float(time_range["end"]["value"]) / float(time_range["end"]["rate"])
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        intervals.append((start, end))
    return intervals


def load_edl(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    payload["kept_intervals"] = merge_intervals(
        (item.get("start", 0), item.get("end", 0))
        for item in payload.get("kept") or []
    )
    return payload
