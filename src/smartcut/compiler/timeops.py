"""时间轴推导：输出时间只由结构顺序决定，模型不做时长算术。"""

from __future__ import annotations

from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.time import (
    RationalTime,
    frame_tolerance,
    sum_rational_times,
)

__all__ = ["frame_tolerance", "total_duration"]


def total_duration(ir: EditIR) -> RationalTime:
    return sum_rational_times([op.range.duration() for op in ir.sequence]).round_to_rate(90_000)
