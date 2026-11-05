"""证据测量：dHash 去重、直方图距离、bbox 映射与评分。

只做 CPU 确定性测量，不产出像素结论、不访问时间线。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO
from typing import Any, Callable

from smartcut.contracts.time import RationalTime
from smartcut.contracts.visual_evidence import (
    Measurement,
    MeasureResult,
    Tile,
    TileMap,
    tile_map_id,
)
from smartcut.errors import PerceptionUnavailable
from smartcut.perception.view import fingerprint


@dataclass(frozen=True)
class TileInput:
    t: RationalTime
    pts: int
    frame_no: int | None
    image_bytes: bytes
    frame_ref: str | None = None


def _bbox_for(
    anchors: list[Any] | None,
    moment: Fraction,
    width: int,
    height: int,
    tolerance: float,
) -> tuple[int, int, int, int] | None:
    if not anchors:
        return None
    limit = Fraction(str(tolerance))
    for anchor in anchors:
        if abs(anchor.t.as_fraction() - moment) > limit:
            continue
        x = int(round(float(anchor.x) * width))
        y = int(round(float(anchor.y) * height))
        w = max(2, int(round(float(anchor.w) * width)))
        h = max(2, int(round(float(anchor.h) * height)))
        x = max(0, min(x, width - 2))
        y = max(0, min(y, height - 2))
        w = max(2, min(w, width - x))
        h = max(2, min(h, height - y))
        return (x, y, w, h)
    return None


def _risk_near(times: list[Fraction], moment: Fraction, tolerance: float) -> bool:
    limit = Fraction(str(tolerance))
    return any(abs(value - moment) <= limit for value in times)


def measure_tiles(
    tiles: list[TileInput],
    *,
    content_id: str,
    time_base: str,
    vfr: bool = False,
    origin_pts: int = 0,
    grid_cols: int = 4,
    label_format: str = "T%02d",
    label_start: int = 1,
    dedupe_distance: int = 4,
    quality_times: list[Fraction] | None = None,
    face_anchors: list[Any] | None = None,
    risk_tolerance_seconds: float = 0.5,
    cancel_check: Callable[[], None] | None = None,
    extra_metrics: list[dict[str, Any]] | None = None,
    engine: str = "cpu",
) -> MeasureResult:
    if not tiles:
        raise ValueError("没有可测量的证据帧")
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - vision optional
        raise PerceptionUnavailable("需要 Pillow；pip install -e '.[vision]'") from exc

    quality_times = quality_times or []
    kept: list[tuple[TileInput, Measurement, bytes, int]] = []
    previous_hist: list[float] | None = None
    for tile_position, item in enumerate(tiles):
        if cancel_check is not None:
            cancel_check()
        try:
            with Image.open(BytesIO(item.image_bytes)) as image:
                canvas = image.convert("RGB")
                width, height = canvas.size
                digest = fingerprint.dhash(canvas)
                histogram = fingerprint.histogram(canvas)
                bbox = _bbox_for(
                    face_anchors,
                    item.t.as_fraction(),
                    width,
                    height,
                    risk_tolerance_seconds,
                )
        except Exception:
            continue
        d_hist = (
            fingerprint.hist_distance(previous_hist, histogram)
            if previous_hist is not None
            else 1.0
        )
        duplicate = dedupe_distance >= 0 and any(
            fingerprint.hamming(digest, other_digest) <= dedupe_distance
            and d_hist < 0.03
            for _, _, _, other_digest in kept
        )
        previous_hist = histogram
        if duplicate:
            continue
        risk = _risk_near(
            quality_times,
            item.t.as_fraction(),
            risk_tolerance_seconds,
        )
        extra = (
            extra_metrics[tile_position]
            if extra_metrics is not None and tile_position < len(extra_metrics)
            else {}
        )
        flow_mag = extra.get("flow_mag")
        flow_dir = extra.get("flow_dir_deg")
        camera_mag = extra.get("camera_mag")
        subject_mag = extra.get("subject_mag")
        camera_motion = extra.get("camera_motion")
        bounded_flow = min(1.0, float(flow_mag or 0.0) / 5.0)
        score = min(
            1.0,
            0.35
            + 0.5 * d_hist
            + (0.15 if bbox is not None else 0.0)
            + (0.15 if risk else 0.0)
            + 0.2 * bounded_flow,
        )
        measurement = Measurement(
            tile_index=len(kept) + 1,
            d_hist=round(d_hist, 6),
            flow_mag=round(float(flow_mag), 6) if flow_mag is not None else None,
            flow_dir_deg=(
                round(float(flow_dir), 3) if flow_dir is not None else None
            ),
            bbox_xywh=bbox,
            width=width,
            height=height,
            score=round(score, 6),
            engine=engine,
            camera_mag=(
                round(float(camera_mag), 6)
                if camera_mag is not None
                else None
            ),
            subject_mag=(
                round(float(subject_mag), 6)
                if subject_mag is not None
                else None
            ),
            camera_motion=(
                str(camera_motion) if camera_motion is not None else None
            ),
        )
        kept.append((item, measurement, item.image_bytes, digest))

    if not kept:
        raise ValueError("证据帧全部不可读或重复")

    grid_cols = max(1, int(grid_cols))
    tile_models: list[Tile] = []
    measurements: list[Measurement] = []
    images: list[bytes] = []
    for index, (item, measurement, image_bytes, _digest) in enumerate(kept, start=1):
        tile_models.append(
            Tile(
                index=index,
                label=label_format % (label_start + index - 1),
                t=item.t,
                pts=item.pts,
                frame_no=item.frame_no,
                row=(index - 1) // grid_cols,
                col=(index - 1) % grid_cols,
                frame_ref=item.frame_ref,
            )
        )
        measurements.append(
            measurement.model_copy(update={"tile_index": index})
        )
        images.append(image_bytes)

    tile_map = TileMap(
        map_id=tile_map_id(content_id, tile_models),
        content_id=content_id,
        time_base=time_base,
        vfr=vfr,
        origin_pts=origin_pts,
        tiles=tile_models,
    )
    return MeasureResult(
        tile_map=tile_map,
        tile_images=images,
        measurements=measurements,
        overlays=[
            {"tile_index": item.tile_index, "bbox_xywh": item.bbox_xywh}
            for item in measurements
            if item.bbox_xywh is not None
        ],
        constants={
            "engine": engine,
            "dedupe_threshold": dedupe_distance,
            "tile_w": 0,
            "tile_h": 0,
            "scene_threshold_pct": 0.0,
            **fingerprint.constants(),
        },
    )


def mean_confidence(measurements: list[Measurement]) -> float:
    if not measurements:
        return 0.0
    return min(1.0, sum(item.score for item in measurements) / len(measurements))


def grid_rows(tile_count: int, grid_cols: int) -> int:
    return max(1, math.ceil(tile_count / max(1, grid_cols)))
