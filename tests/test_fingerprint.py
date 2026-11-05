from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from smartcut.perception.view import fingerprint
from smartcut.perception.view.hash import compare_semantic, semantic_hash


def _image(offset: int) -> Image.Image:
    image = Image.new("RGB", (160, 90), (20, 30, 50))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20 + offset, 20, 80 + offset, 70), fill=(240, 240, 240))
    return image


def test_fingerprint_hash_and_histogram_are_stable() -> None:
    image = _image(0)
    same = _image(0)
    assert fingerprint.dhash(image) == fingerprint.dhash(same)
    assert fingerprint.hamming(
        fingerprint.dhash(image), fingerprint.dhash(_image(40))
    ) > 0
    histogram = fingerprint.histogram(image)
    assert len(histogram) == fingerprint.HISTOGRAM_BINS
    assert abs(sum(histogram) - 1.0) < 0.001
    assert fingerprint.engine() in {"cv2", "pillow"}


def test_semantic_hash_is_versioned_and_comparable() -> None:
    image = _image(0)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    payload = buffer.getvalue()
    digest = semantic_hash(payload, grid_rows=1, grid_cols=1)
    assert digest.startswith("dhash:v2:")
    comparison = compare_semantic(
        payload,
        payload,
        grid_rows=1,
        grid_cols=1,
    )
    assert comparison["within_tolerance"]
    assert comparison["max_distance"] == 0
