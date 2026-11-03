"""响度目标解析与两遍 loudnorm 测量结果解析。"""

from __future__ import annotations

import json

from smartcut.contracts.loudness import parse_target_lufs

__all__ = ["parse_loudnorm_measure", "parse_target_lufs"]


def parse_loudnorm_measure(stderr: str) -> dict[str, str]:
    """从 loudnorm 第一遍输出的 stderr JSON 中提取 measured_* 字段。"""
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("loudnorm 测量输出中没有 JSON")
    payload = json.loads(stderr[start : end + 1])
    mapping = {
        "measured_I": "input_i",
        "measured_TP": "input_tp",
        "measured_LRA": "input_lra",
        "measured_thresh": "input_thresh",
        "offset": "target_offset",
    }
    result: dict[str, str] = {}
    for out_key, in_key in mapping.items():
        value = payload.get(in_key)
        if value in (None, ""):
            continue
        result[out_key] = str(value)
    return result
