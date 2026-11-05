"""统一基础指纹：dHash 与灰度直方图。

- cv2 可用时用 cv2 作为主实现；缺失时回退 Pillow（结果可能不同）。
- 引擎与版本必须进入证据 constants / 缓存键，禁止静默换口径。
"""

from __future__ import annotations

from typing import Any

FINGERPRINT_VERSION = "dhash:v2"
HISTOGRAM_BINS = 16


def _cv():
    try:
        import cv2
        import numpy as np
    except ImportError:  # pragma: no cover - vision optional
        return None, None
    return cv2, np


def engine() -> str:
    cv2, _np = _cv()
    return "cv2" if cv2 is not None else "pillow"


def _hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def hamming(left: int, right: int) -> int:
    return _hamming(left, right)


def dhash_gray(gray: Any, cv2: Any, np: Any) -> int:
    """cv2 灰度图 → 64-bit dHash。"""
    resized = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (resized[:, 1:] > resized[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def dhash(image: Any) -> int:
    """PIL Image → 64-bit dHash；优先 cv2，缺失时回退 Pillow。"""
    cv2, np = _cv()
    if cv2 is not None and np is not None:
        rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        return dhash_gray(gray, cv2, np)
    from PIL import Image

    gray = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = gray.tobytes()
    value = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            value = (value << 1) | int(
                pixels[offset + column] > pixels[offset + column + 1]
            )
    return value


def histogram_gray(gray: Any, cv2: Any, np: Any, bins: int = HISTOGRAM_BINS) -> list[float]:
    counts = cv2.calcHist([gray], [0], None, [bins], [0, 256]).flatten()
    total = float(counts.sum()) or 1.0
    return [float(value) / total for value in counts]


def histogram(image: Any, bins: int = HISTOGRAM_BINS) -> list[float]:
    """PIL Image → 归一化灰度直方图；优先 cv2，缺失时回退 Pillow。"""
    cv2, np = _cv()
    if cv2 is not None and np is not None:
        rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        return histogram_gray(gray, cv2, np, bins)
    gray = image.convert("L")
    buckets = gray.histogram()
    total = max(1, sum(buckets))
    step = len(buckets) / bins
    result = []
    for index in range(bins):
        start = int(index * step)
        end = max(start + 1, int((index + 1) * step))
        result.append(sum(buckets[start:end]) / total)
    return result


def hist_distance(left: list[float], right: list[float]) -> float:
    return min(1.0, sum(abs(a - b) for a, b in zip(left, right)) / 2.0)


def constants() -> dict[str, str]:
    return {
        "fingerprint_version": FINGERPRINT_VERSION,
        "fingerprint_engine": engine(),
    }
