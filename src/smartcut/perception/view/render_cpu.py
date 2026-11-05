"""Pillow 参考渲染：固定网格、编号、bbox；CI/headless 默认后端。"""

from __future__ import annotations

import hashlib
from io import BytesIO

from smartcut.contracts.visual_evidence import (
    MeasureResult,
    RenderedImage,
    ViewRequest,
)
from smartcut.errors import PerceptionUnavailable

BACKGROUND = "#111827"
LABEL_BG = "#0f172a"
LABEL_FG = "#f8fafc"
BBOX_COLOR = "#e5484d"
PAD = 4


def _font():
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=14)
    except TypeError:  # pragma: no cover - 旧 Pillow
        return ImageFont.load_default()


def _draw_bbox(draw, measurement, cell, tile_size) -> None:
    if measurement.bbox_xywh is None:
        return
    width = max(1, measurement.width)
    height = max(1, measurement.height)
    tile_width, tile_height = tile_size
    scale = max(tile_width / width, tile_height / height)
    cropped_width = tile_width / scale
    cropped_height = tile_height / scale
    left = (width - cropped_width) / 2
    top = (height - cropped_height) / 2
    x, y, w, h = measurement.bbox_xywh
    x0 = cell[0] + int(round((x - left) * scale))
    y0 = cell[1] + int(round((y - top) * scale))
    x1 = cell[0] + int(round((x + w - left) * scale))
    y1 = cell[1] + int(round((y + h - top) * scale))
    draw.rectangle((x0, y0, x1, y1), outline=BBOX_COLOR, width=3)


class CpuEvidenceRenderer:
    backend = "cpu"

    def __init__(self) -> None:
        try:
            import PIL
        except ImportError as exc:  # pragma: no cover
            raise PerceptionUnavailable(
                "需要 Pillow；pip install -e '.[vision]'"
            ) from exc
        self.shader_hash = f"cpu:pillow/{PIL.__version__}"
        self.device_tier = "cpu"

    def render(self, measure: MeasureResult, request: ViewRequest) -> RenderedImage:
        return self._render(measure, request, {})

    def render_many(
        self,
        measure: MeasureResult,
        requests: list[ViewRequest],
    ) -> list[RenderedImage]:
        """同一 measure 的多个网格/kind 请求共享解码后的图片。"""
        image_cache: dict[str, object] = {}
        return [
            self._render(measure, request, image_cache)
            for request in requests
        ]

    def _render(
        self,
        measure: MeasureResult,
        request: ViewRequest,
        image_cache: dict[str, object],
    ) -> RenderedImage:
        try:
            from PIL import Image, ImageDraw, ImageOps
        except ImportError as exc:  # pragma: no cover
            raise PerceptionUnavailable("需要 Pillow 渲染证据") from exc

        tile_width = request.tile_width
        tile_height = request.tile_height
        grid_cols = max(1, request.grid_cols)
        rows = (len(measure.tile_map.tiles) + grid_cols - 1) // grid_cols
        canvas_width = grid_cols * (tile_width + PAD) + PAD
        canvas_height = rows * (tile_height + PAD) + PAD
        canvas = Image.new("RGB", (canvas_width, canvas_height), BACKGROUND)
        draw = ImageDraw.Draw(canvas)
        font = _font()

        for tile, image_bytes, measurement in zip(
            measure.tile_map.tiles,
            measure.tile_images,
            measure.measurements,
        ):
            cache_key = hashlib.sha256(image_bytes).hexdigest()
            image = image_cache.get(cache_key)
            if image is None:
                try:
                    with Image.open(BytesIO(image_bytes)) as source:
                        image = source.convert("RGB")
                except Exception:
                    continue
                image_cache[cache_key] = image
            try:
                fitted = ImageOps.fit(
                    image,
                    (tile_width, tile_height),
                    method=Image.Resampling.LANCZOS,
                )
            except Exception:
                continue
            index = tile.index - 1
            row = index // grid_cols
            col = index % grid_cols
            x = PAD + col * (tile_width + PAD)
            y = PAD + row * (tile_height + PAD)
            canvas.paste(fitted, (x, y))
            cell = (x, y, x + tile_width, y + tile_height)
            _draw_bbox(
                draw,
                measurement,
                cell,
                (tile_width, tile_height),
            )
            label_width = max(42, 10 + len(tile.label) * 9)
            draw.rectangle(
                (x + 4, y + 4, x + label_width, y + 24),
                fill=LABEL_BG,
            )
            draw.text((x + 8, y + 6), tile.label, fill=LABEL_FG, font=font)

        buffer = BytesIO()
        canvas.save(buffer, format="PNG", optimize=True)
        return RenderedImage(
            png_bytes=buffer.getvalue(),
            width=canvas_width,
            height=canvas_height,
            backend=self.backend,
            shader_hash=self.shader_hash,
            device_tier=self.device_tier,
        )
