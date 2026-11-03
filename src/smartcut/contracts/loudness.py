"""响度目标解析：纯函数，Compiler 与 Planner 共用。"""

from __future__ import annotations

import re

_TARGET = re.compile(r"-?\d+(?:\.\d+)?")


def parse_target_lufs(value: str) -> float:
    match = _TARGET.search(value)
    if match is None:
        raise ValueError(f"无法解析响度目标: {value!r}")
    target = float(match.group())
    if not -70.0 <= target <= 0.0:
        raise ValueError(f"响度目标超出范围: {target}")
    return target
