"""按渲染后端分档的证据预算；预算不足时优先保证 CPU 可用性。"""

from __future__ import annotations

from dataclasses import dataclass

from smartcut.config import Settings


@dataclass(frozen=True)
class ViewBudget:
    max_images: int
    tile_width: int
    tile_height: int


def view_budget(settings: Settings, backend: str) -> ViewBudget:
    max_images = max(1, int(settings.view_max_images))
    if backend == "qtgl":
        return ViewBudget(
            max_images=max_images,
            tile_width=int(settings.view_tile_width),
            tile_height=int(settings.view_tile_height),
        )
    # CPU/Pillow：限制 tile 数与像素面积，避免证据生成拖慢主链路。
    return ViewBudget(
        max_images=min(max_images, 5),
        tile_width=min(int(settings.view_tile_width), 320),
        tile_height=min(int(settings.view_tile_height), 180),
    )
