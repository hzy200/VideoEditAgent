"""对比 CPU Pillow 与 Qt OpenGL 证据拼版耗时。

用法：
    python scripts/benchmark_evidence_render.py --tiles 9 --repeat 3 \
        --assert-not-slower
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from io import BytesIO

os.environ.setdefault("QT_QPA_PLATFORM", "windows")


def _images(count: int) -> list[bytes]:
    from PIL import Image, ImageDraw

    output = []
    for index in range(count):
        image = Image.new("RGB", (320, 180), (20 + index * 9 % 200, 40, 80))
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 120, 80), fill=(255, 255, 255))
        draw.text((30, 100), f"T{index + 1:02d}", fill=(0, 0, 0))
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=90)
        output.append(buffer.getvalue())
    return output


def _measure(count: int, kind: str = "contact_sheet"):
    from smartcut.contracts.time import RationalTime
    from smartcut.perception.view.measure import TileInput, measure_tiles
    from smartcut.perception.view.views import build_flow_strip, build_temporal_diff

    tiles = [
        TileInput(
            t=RationalTime.from_seconds(str(index * 2), rate=90_000),
            pts=index * 60,
            frame_no=index * 60,
            image_bytes=payload,
        )
        for index, payload in enumerate(_images(count))
    ]
    built = None
    if kind == "temporal_diff":
        built = build_temporal_diff(tiles, tile_width=384, tile_height=216)
    elif kind == "flow_strip":
        built = build_flow_strip(tiles, tile_width=384, tile_height=216)
    if built is not None:
        return measure_tiles(
            built.tiles,
            content_id="benchmark",
            time_base="1/30",
            grid_cols=4,
            dedupe_distance=-1,
            extra_metrics=built.extra_metrics,
            engine="cpu-flow",
        )
    return measure_tiles(
        tiles,
        content_id="benchmark",
        time_base="1/30",
        grid_cols=4,
        dedupe_distance=0,
    )


def _request(count: int, kind: str = "contact_sheet"):
    from smartcut.contracts.time import RationalTime
    from smartcut.contracts.visual_evidence import ViewRequest

    return ViewRequest(
        kind=kind,
        content_id="benchmark",
        source_range=(
            RationalTime.from_seconds("0", rate=90_000),
            RationalTime.from_seconds(str(max(1, count * 2)), rate=90_000),
        ),
        grid_cols=4,
        tile_width=384,
        tile_height=216,
    )


def _bench(renderer, measure, request, repeat: int) -> float:
    renderer.render(measure, request)  # 预热
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        renderer.render(measure, request)
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="证据渲染 CPU vs QtGL 基准")
    parser.add_argument("--tiles", type=int, default=9)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument(
        "--kind",
        choices=("contact_sheet", "temporal_diff", "flow_strip", "bbox_overlay"),
        default="contact_sheet",
    )
    parser.add_argument("--assert-not-slower", action="store_true")
    args = parser.parse_args(argv)

    from PySide6.QtWidgets import QApplication

    from smartcut.perception.view.render_cpu import CpuEvidenceRenderer

    app = QApplication.instance() or QApplication([])
    measure = _measure(max(1, args.tiles), args.kind)
    request = _request(max(1, args.tiles), args.kind)
    payload = {
        "kind": args.kind,
        "tiles": len(measure.tile_map.tiles),
        "cpu_ms": round(_bench(CpuEvidenceRenderer(), measure, request, args.repeat), 3),
    }
    try:
        from smartcut.desktop.view.render_qtgl import QtEvidenceRenderer

        payload["gl_ms"] = round(
            _bench(QtEvidenceRenderer(), measure, request, args.repeat),
            3,
        )
        payload["backend"] = "qtgl"
    except Exception as exc:
        payload["backend"] = "cpu"
        payload["gl_error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.assert_not_slower and "gl_ms" in payload:
        if payload["gl_ms"] > payload["cpu_ms"] * 3 + 30:
            print("GL 渲染明显慢于 CPU", file=sys.stderr)
            return 1
    return 0 if payload["backend"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
