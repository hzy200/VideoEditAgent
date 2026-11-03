"""桌面展示/解析共用的时间辅助。"""

from __future__ import annotations

from typing import Any

from smartcut.contracts.time import seconds_from_mapping


def seconds_or_none(value: Any) -> float | None:
    """解析 RationalTime 字典或数字；无法解析时返回 None。"""
    if isinstance(value, dict):
        return seconds_from_mapping(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def format_seconds(value: Any, *, digits: int = 3) -> str:
    seconds = seconds_or_none(value)
    return "-" if seconds is None else f"{seconds:.{digits}f}"
