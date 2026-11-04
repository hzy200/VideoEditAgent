"""M2 证据视图构建：帧差/光流条带/bbox/codec residual/ROI。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from smartcut.perception.view.flow import (
    FlowResult,
    crop_roi,
    flow_between,
    flow_strip,
    temporal_composite,
)
from smartcut.perception.view.measure import TileInput


@dataclass
class BuiltView:
    tiles: list[TileInput]
    extra_metrics: list[dict[str, Any]] = field(default_factory=list)
    constants: dict[str, Any] = field(default_factory=dict)


def build_temporal_diff(
    tiles: list[TileInput],
    *,
    tile_width: int,
    tile_height: int,
) -> BuiltView:
    if len(tiles) < 2:
        return BuiltView(tiles=tiles, extra_metrics=[{} for _ in tiles])
    output: list[TileInput] = []
    metrics: list[dict[str, Any]] = []
    constants: dict[str, Any] = {}
    for previous, current in zip(tiles, tiles[1:]):
        composite, flow = temporal_composite(
            previous.image_bytes,
            current.image_bytes,
            tile_width,
            tile_height,
        )
        output.append(
            TileInput(
                t=current.t,
                pts=current.pts,
                frame_no=current.frame_no,
                image_bytes=composite,
                frame_ref=current.frame_ref,
            )
        )
        metrics.append(
            {"flow_mag": flow.flow_mag, "flow_dir_deg": flow.flow_dir_deg}
        )
        constants = {
            "flow_mag_cap": flow.mag_cap,
            "flow_mag_floor": flow.mag_floor,
            "flow_encoding": "hsv_hue_v1",
        }
    return BuiltView(tiles=output, extra_metrics=metrics, constants=constants)


def build_flow_strip(
    tiles: list[TileInput],
    *,
    tile_width: int,
    tile_height: int,
    group_size: int = 4,
) -> BuiltView:
    if len(tiles) < 2:
        return BuiltView(tiles=tiles, extra_metrics=[{} for _ in tiles])
    flows: list[FlowResult] = [
        flow_between(previous.image_bytes, current.image_bytes)
        for previous, current in zip(tiles, tiles[1:])
    ]
    output: list[TileInput] = []
    metrics: list[dict[str, Any]] = []
    group_size = max(1, min(4, group_size))
    for start in range(0, len(flows), group_size):
        group = flows[start : start + group_size]
        if not group:
            continue
        anchor = tiles[min(start + 1, len(tiles) - 1)]
        output.append(
            TileInput(
                t=anchor.t,
                pts=anchor.pts,
                frame_no=anchor.frame_no,
                image_bytes=flow_strip(group, tile_width, tile_height),
                frame_ref=anchor.frame_ref,
            )
        )
        metrics.append(
            {
                "flow_mag": round(sum(item.flow_mag for item in group) / len(group), 6),
                "flow_dir_deg": round(
                    sum(item.flow_dir_deg for item in group) / len(group), 3
                ),
            }
        )
    constants = {
        "flow_mag_cap": flows[-1].mag_cap,
        "flow_mag_floor": flows[-1].mag_floor,
        "flow_encoding": "hsv_hue_v1",
    }
    return BuiltView(tiles=output, extra_metrics=metrics, constants=constants)


def build_unchanged(tiles: list[TileInput]) -> BuiltView:
    return BuiltView(tiles=tiles, extra_metrics=[{} for _ in tiles])


def build_bbox_overlay(tiles: list[TileInput]) -> BuiltView:
    """bbox 由 measure/render 叠加；这里只保留原始帧。"""
    return build_unchanged(tiles)


def build_roi(
    tile: TileInput,
    rect: tuple[float, float, float, float],
    *,
    target: int = 512,
) -> BuiltView:
    image_bytes, box = crop_roi(tile.image_bytes, rect, target=target)
    return BuiltView(
        tiles=[
            TileInput(
                t=tile.t,
                pts=tile.pts,
                frame_no=tile.frame_no,
                image_bytes=image_bytes,
                frame_ref=tile.frame_ref,
            )
        ],
        extra_metrics=[{}],
        constants={"roi_box": box, "roi_target": target},
    )
