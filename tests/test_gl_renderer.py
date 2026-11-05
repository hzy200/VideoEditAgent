from __future__ import annotations

import os
import time
from io import BytesIO

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("PIL")

from PIL import Image, ImageDraw
from PySide6.QtWidgets import QApplication

from smartcut.contracts.time import RationalTime
from smartcut.contracts.visual_evidence import ViewRequest
from smartcut.perception.view.hash import compare_semantic
from smartcut.perception.view.measure import TileInput, measure_tiles
from smartcut.perception.view.render_cpu import CpuEvidenceRenderer


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _image(color: tuple[int, int, int], text: str) -> bytes:
    image = Image.new("RGB", (320, 180), color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 120, 80), fill=(255, 255, 255))
    draw.text((30, 100), text, fill=(0, 0, 0))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _measure():
    return measure_tiles(
        [
            TileInput(t=_rt("0"), pts=0, frame_no=0, image_bytes=_image((20, 20, 80), "A")),
            TileInput(t=_rt("2"), pts=60, frame_no=60, image_bytes=_image((80, 20, 20), "B")),
            TileInput(t=_rt("4"), pts=120, frame_no=120, image_bytes=_image((20, 80, 20), "C")),
        ],
        content_id="p_gl",
        time_base="1/30",
        grid_cols=2,
    )


def _request() -> ViewRequest:
    return ViewRequest(
        content_id="p_gl",
        source_range=(_rt("0"), _rt("4")),
        grid_cols=2,
        tile_width=320,
        tile_height=180,
    )


def test_qtgl_matches_cpu_semantic_hash(app) -> None:
    from smartcut.desktop.view.render_qtgl import QtEvidenceRenderer

    measure = _measure()
    request = _request()
    cpu = CpuEvidenceRenderer().render(measure, request)
    try:
        gl = QtEvidenceRenderer().render(measure, request)
    except Exception as exc:  # 无 GL/RDP/驱动不可用
        pytest.skip(f"无可用 OpenGL 上下文: {type(exc).__name__}: {exc}")
    assert gl.backend == "qtgl"
    comparison = compare_semantic(
        cpu.png_bytes,
        gl.png_bytes,
        grid_rows=2,
        grid_cols=2,
    )
    assert comparison["within_tolerance"], comparison
    assert comparison["max_distance"] <= 6
    assert comparison["channel_mean_diff"] <= 3


def test_qtgl_render_many_matches_cpu(app) -> None:
    from smartcut.desktop.view.render_qtgl import QtEvidenceRenderer

    measure = _measure()
    requests = [
        _request(),
        _request().model_copy(update={"kind": "bbox_overlay", "grid_cols": 1}),
    ]
    try:
        gl_items = QtEvidenceRenderer().render_many(measure, requests)
    except Exception as exc:  # 无 GL/RDP/驱动不可用
        pytest.skip(f"无可用 OpenGL 上下文: {type(exc).__name__}: {exc}")
    cpu_items = CpuEvidenceRenderer().render_many(measure, requests)
    assert len(gl_items) == len(cpu_items) == 2
    for gl, cpu in zip(gl_items, cpu_items):
        comparison = compare_semantic(
            cpu.png_bytes,
            gl.png_bytes,
            grid_rows=1,
            grid_cols=1,
        )
        assert comparison["within_tolerance"], comparison


def test_qtgl_not_slower_than_cpu(app) -> None:
    from smartcut.desktop.view.render_qtgl import QtEvidenceRenderer

    measure = _measure()
    request = _request()
    cpu_renderer = CpuEvidenceRenderer()
    gl_renderer = QtEvidenceRenderer()
    try:
        gl_renderer.render(measure, request)  # 上下文/首帧预热
    except Exception as exc:
        pytest.skip(f"无可用 OpenGL 上下文: {type(exc).__name__}: {exc}")

    def elapsed(renderer) -> float:
        started = time.perf_counter()
        for _ in range(3):
            renderer.render(measure, request)
        return (time.perf_counter() - started) / 3

    cpu_ms = elapsed(cpu_renderer) * 1000
    gl_ms = elapsed(gl_renderer) * 1000
    # 相对断言，容忍 CI/驱动抖动。
    assert gl_ms <= cpu_ms * 3 + 30, (cpu_ms, gl_ms)
