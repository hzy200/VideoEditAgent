"""EditIR 纯函数差异：按源区间比较两个 revision 的片段。"""

from __future__ import annotations

from fractions import Fraction

from smartcut.contracts.edit_ir import EditIR, TrimOp


def _key(op: TrimOp) -> tuple[str, Fraction, Fraction]:
    return (
        op.src,
        op.range.start.as_fraction(),
        op.range.end.as_fraction(),
    )


def _item(op: TrimOp) -> dict:
    return {
        "src": op.src,
        "start_seconds": op.range.start.to_ffmpeg_seconds(),
        "end_seconds": op.range.end.to_ffmpeg_seconds(),
        "duration_seconds": op.range.duration().to_ffmpeg_seconds(),
        "output_seconds": op.output_duration().to_ffmpeg_seconds(),
        "speed": op.speed,
        "tag": op.tag,
        "confidence": op.confidence,
    }


def diff_sequences(base: EditIR, target: EditIR) -> dict:
    """按 (src, start, end) 做多重集合比较，保留目标顺序。"""
    remaining: dict[tuple[str, Fraction, Fraction], list[TrimOp]] = {}
    for op in base.sequence:
        remaining.setdefault(_key(op), []).append(op)

    added: list[TrimOp] = []
    unchanged: list[TrimOp] = []
    for op in target.sequence:
        bucket = remaining.get(_key(op))
        if bucket:
            bucket.pop()
            unchanged.append(op)
        else:
            added.append(op)
    removed = [op for bucket in remaining.values() for op in bucket]
    removed.sort(key=lambda op: (op.range.start.as_fraction(), op.range.end.as_fraction()))

    return {
        "base_revision": base.revision,
        "revision": target.revision,
        "added": [_item(op) for op in added],
        "removed": [_item(op) for op in removed],
        "unchanged": [_item(op) for op in unchanged],
        "added_count": len(added),
        "removed_count": len(removed),
        "unchanged_count": len(unchanged),
        "effects_changed": base.effects != target.effects,
        "transitions_changed": base.transitions != target.transitions,
        "overlays_changed": base.overlays != target.overlays,
        "removals_changed": base.removals != target.removals,
        "audio_changed": base.audio != target.audio,
        "export_changed": base.export != target.export,
        "base_duration_seconds": base.total_duration().to_ffmpeg_seconds(),
        "duration_seconds": target.total_duration().to_ffmpeg_seconds(),
    }


def diff_ir(base: EditIR, target: EditIR) -> dict:
    return diff_sequences(base, target)
