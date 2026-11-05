"""证据渲染哈希：render_hash 严格、semantic_hash 跨后端容差。"""

from __future__ import annotations

import hashlib
from io import BytesIO

from smartcut.contracts.visual_evidence import canonical_bytes
from smartcut.perception.view import fingerprint


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _dhash_64(image) -> int:
    return fingerprint.dhash(image)


def semantic_hash(
    png_bytes: bytes,
    *,
    grid_rows: int,
    grid_cols: int,
) -> str:
    """按 grid 切块后逐块 dHash；用于 CPU/GL 语义比对，不承诺 bit-identical。"""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 Pillow 计算 semantic_hash") from exc
    if grid_rows <= 0 or grid_cols <= 0:
        raise ValueError("grid_rows/grid_cols 必须为正")
    with Image.open(BytesIO(png_bytes)) as image:
        canvas = image.convert("RGB")
        width, height = canvas.size
        values: list[int] = []
        for row in range(grid_rows):
            top = round(height * row / grid_rows)
            bottom = round(height * (row + 1) / grid_rows)
            for col in range(grid_cols):
                left = round(width * col / grid_cols)
                right = round(width * (col + 1) / grid_cols)
                cell = canvas.crop((left, top, max(left + 1, right), max(top + 1, bottom)))
                values.append(_dhash_64(cell))
    version = fingerprint.FINGERPRINT_VERSION.replace("dhash:", "")
    return f"dhash:{version}:" + "".join(f"{value:016x}" for value in values)


def compare_semantic(
    left_png: bytes,
    right_png: bytes,
    *,
    grid_rows: int,
    grid_cols: int,
    max_distance: int = 6,
    channel_mean_tolerance: float = 3.0,
) -> dict:
    """CPU/GL 一致性检查；返回逐块汉明距离与整图通道均值差。"""
    left = semantic_hash(left_png, grid_rows=grid_rows, grid_cols=grid_cols)
    right = semantic_hash(right_png, grid_rows=grid_rows, grid_cols=grid_cols)
    left_values = _decode(left)
    right_values = _decode(right)
    distances = [
        (lvalue ^ rvalue).bit_count()
        for lvalue, rvalue in zip(left_values, right_values)
    ]
    channel_diff = _channel_mean_diff(left_png, right_png)
    return {
        "tile_distances": distances,
        "max_distance": max(distances) if distances else 0,
        "within_tolerance": bool(
            distances
            and max(distances) <= max_distance
            and channel_diff <= channel_mean_tolerance
        ),
        "channel_mean_diff": round(channel_diff, 4),
        "left": left,
        "right": right,
    }


def _decode(value: str) -> list[int]:
    text = value.rsplit(":", 1)[-1]
    return [int(text[index : index + 16], 16) for index in range(0, len(text), 16)]


def _channel_mean_diff(left_png: bytes, right_png: bytes) -> float:
    try:
        from PIL import Image, ImageChops
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 Pillow 比较语义哈希") from exc
    with Image.open(BytesIO(left_png)) as left, Image.open(
        BytesIO(right_png)
    ) as right:
        a = left.convert("RGB").resize((64, 64))
        b = right.convert("RGB").resize((64, 64))
        diff = ImageChops.difference(a, b)
        histograms = diff.histogram()
    total = 64 * 64
    means = []
    for channel in range(3):
        values = histograms[channel * 256 : (channel + 1) * 256]
        means.append(sum(index * count for index, count in enumerate(values)) / total)
    return sum(means) / 3


def stable_bytes_hash(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
