"""Qt OpenGL 离屏证据渲染：只做拼版/编号/bbox，禁止重算光流。

每个渲染线程独立 QOffscreenSurface + QOpenGLContext；创建失败或驱动异常由
服务层捕获并回退 CPU Pillow 渲染。
"""

from __future__ import annotations

import threading

from smartcut.contracts.visual_evidence import (
    MeasureResult,
    RenderedImage,
    ViewRequest,
)
from smartcut.perception.view.render_cpu import (
    BACKGROUND,
    BBOX_COLOR,
    LABEL_BG,
    LABEL_FG,
    PAD,
)

_STATE = threading.local()
SHADER_HASH = "qtgl:painter-v1"


class QtGLUnavailable(RuntimeError):
    pass


def _state() -> tuple[object, object]:
    surface = getattr(_STATE, "surface", None)
    context = getattr(_STATE, "context", None)
    if surface is not None and context is not None:
        return surface, context
    try:
        from PySide6.QtGui import (
            QGuiApplication,
            QOffscreenSurface,
            QOpenGLContext,
            QSurfaceFormat,
        )
    except ImportError as exc:  # pragma: no cover - desktop extra 缺失
        raise QtGLUnavailable("未安装 PySide6") from exc
    if QGuiApplication.instance() is None:
        raise QtGLUnavailable("QGuiApplication 尚未创建")
    fmt = QSurfaceFormat.defaultFormat()
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    fmt.setDepthBufferSize(0)
    fmt.setStencilBufferSize(0)
    surface = QOffscreenSurface()
    surface.setFormat(fmt)
    surface.create()
    if not surface.isValid():
        raise QtGLUnavailable("QOffscreenSurface 创建失败")
    context = QOpenGLContext()
    context.setFormat(fmt)
    context.create()
    if not context.isValid():
        raise QtGLUnavailable("QOpenGLContext 创建失败")
    if not context.makeCurrent(surface):
        raise QtGLUnavailable("QOpenGLContext 无法设为当前上下文")
    _STATE.surface = surface
    _STATE.context = context
    return surface, context


def _font():
    from PySide6.QtGui import QFont

    font = QFont("Segoe UI")
    font.setPixelSize(14)
    return font


def _qimage_from_bytes(data: bytes):
    from PySide6.QtGui import QImage

    image = QImage.fromData(data, "JPEG")
    if image.isNull():
        image = QImage.fromData(data)
    return image


def _draw_bbox(painter, measurement, x: float, y: float, tile_size) -> None:
    if measurement.bbox_xywh is None:
        return
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QPen

    width = max(1, measurement.width)
    height = max(1, measurement.height)
    tile_width, tile_height = tile_size
    scale = max(tile_width / width, tile_height / height)
    cropped_width = tile_width / scale
    cropped_height = tile_height / scale
    left = (width - cropped_width) / 2
    top = (height - cropped_height) / 2
    bx, by, bw, bh = measurement.bbox_xywh
    painter.setPen(QPen(QColor(BBOX_COLOR), 3))
    painter.setBrush(QColor(0, 0, 0, 0))
    painter.drawRect(
        QRectF(
            x + (bx - left) * scale,
            y + (by - top) * scale,
            bw * scale,
            bh * scale,
        )
    )


class QtEvidenceRenderer:
    backend = "qtgl"

    def __init__(self) -> None:
        self.shader_hash = SHADER_HASH
        self.device_tier = "gpu"

    def render(self, measure: MeasureResult, request: ViewRequest) -> RenderedImage:
        try:
            from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt
            from PySide6.QtGui import QColor, QImage, QPainter
            from PySide6.QtOpenGL import (
                QOpenGLFramebufferObject,
                QOpenGLPaintDevice,
            )
        except ImportError as exc:  # pragma: no cover
            raise QtGLUnavailable("Qt OpenGL 模块不可用") from exc

        surface, context = _state()
        if not context.makeCurrent(surface):
            raise QtGLUnavailable("QOpenGLContext 无法重新设为当前上下文")

        tile_width = request.tile_width
        tile_height = request.tile_height
        grid_cols = max(1, request.grid_cols)
        rows = (len(measure.tile_map.tiles) + grid_cols - 1) // grid_cols
        canvas_width = grid_cols * (tile_width + PAD) + PAD
        canvas_height = rows * (tile_height + PAD) + PAD

        framebuffer = QOpenGLFramebufferObject(canvas_width, canvas_height)
        if not framebuffer.isValid() or not framebuffer.bind():
            raise QtGLUnavailable("QOpenGLFramebufferObject 创建/绑定失败")
        painter = None
        try:
            device = QOpenGLPaintDevice(canvas_width, canvas_height)
            painter = QPainter(device)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.fillRect(
                QRectF(0, 0, canvas_width, canvas_height),
                QColor(BACKGROUND),
            )
            painter.setFont(_font())
            for tile, image_bytes, measurement in zip(
                measure.tile_map.tiles,
                measure.tile_images,
                measure.measurements,
            ):
                image = _qimage_from_bytes(image_bytes)
                if image.isNull():
                    continue
                index = tile.index - 1
                row = index // grid_cols
                col = index % grid_cols
                x = PAD + col * (tile_width + PAD)
                y = PAD + row * (tile_height + PAD)
                source_width = max(1, image.width())
                source_height = max(1, image.height())
                scale = max(
                    tile_width / source_width,
                    tile_height / source_height,
                )
                crop_width = tile_width / scale
                crop_height = tile_height / scale
                source_rect = QRectF(
                    (source_width - crop_width) / 2,
                    (source_height - crop_height) / 2,
                    crop_width,
                    crop_height,
                )
                painter.save()
                painter.setRenderHint(
                    QPainter.RenderHint.SmoothPixmapTransform,
                    True,
                )
                painter.drawImage(
                    QRectF(x, y, tile_width, tile_height),
                    image,
                    source_rect,
                )
                painter.restore()
                _draw_bbox(
                    painter,
                    measurement,
                    x,
                    y,
                    (tile_width, tile_height),
                )
                label_width = max(42, 10 + len(tile.label) * 9)
                painter.fillRect(
                    QRectF(x + 4, y + 4, label_width, 20),
                    QColor(LABEL_BG),
                )
                painter.setPen(QColor(LABEL_FG))
                painter.drawText(
                    QRectF(x + 8, y + 4, label_width - 4, 20),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    tile.label,
                )
        finally:
            if painter is not None:
                painter.end()
            framebuffer.release()

        image = framebuffer.toImage()
        if image.isNull():
            raise QtGLUnavailable("QOpenGLFramebufferObject.toImage() 返回空图")
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise QtGLUnavailable("QImage PNG 编码失败")
        return RenderedImage(
            png_bytes=bytes(buffer.data()),
            width=canvas_width,
            height=canvas_height,
            backend=self.backend,
            shader_hash=self.shader_hash,
            device_tier=self.device_tier,
        )


def render_selfcheck(
    *,
    width: int = 64,
    height: int = 64,
) -> dict:
    """桌面自检：64x64 离屏渲染；失败返回结构化降级结果。"""
    try:
        from PySide6.QtCore import QBuffer, QIODevice, QRectF
        from PySide6.QtGui import QColor, QImage, QPainter
        from PySide6.QtOpenGL import (
            QOpenGLFramebufferObject,
            QOpenGLPaintDevice,
        )
    except ImportError as exc:  # pragma: no cover
        return {"ok": False, "backend": "cpu", "error": str(exc)}
    try:
        surface, context = _state()
        if not context.makeCurrent(surface):
            raise QtGLUnavailable("makeCurrent 失败")
        framebuffer = QOpenGLFramebufferObject(width, height)
        if not framebuffer.isValid() or not framebuffer.bind():
            raise QtGLUnavailable("FBO 创建失败")
        device = QOpenGLPaintDevice(width, height)
        painter = QPainter(device)
        try:
            painter.fillRect(
                QRectF(0, 0, width, height),
                QColor("#111827"),
            )
            painter.fillRect(QRectF(8, 8, 20, 20), QColor("#f8fafc"))
        finally:
            painter.end()
            framebuffer.release()
        image = framebuffer.toImage()
        if image.isNull():
            raise QtGLUnavailable("空图")
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise QtGLUnavailable("PNG 编码失败")
        png_bytes = bytes(buffer.data())
        semantic: dict = {"semantic_match": None}
        try:
            from io import BytesIO

            from PIL import Image, ImageDraw

            from smartcut.perception.view.hash import compare_semantic

            cpu = Image.new("RGB", (width, height), "#111827")
            draw = ImageDraw.Draw(cpu)
            draw.rectangle((8, 8, 28, 28), fill="#f8fafc")
            cpu_buffer = BytesIO()
            cpu.save(cpu_buffer, format="PNG")
            comparison = compare_semantic(
                png_bytes,
                cpu_buffer.getvalue(),
                grid_rows=1,
                grid_cols=1,
            )
            semantic = {
                "semantic_match": comparison["within_tolerance"],
                "channel_mean_diff": comparison["channel_mean_diff"],
                "tile_distance": comparison["max_distance"],
            }
        except Exception as exc:
            semantic = {
                "semantic_match": None,
                "semantic_error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "ok": True,
            "backend": "qtgl",
            "width": image.width(),
            "height": image.height(),
            "bytes": len(png_bytes),
            "shader_hash": SHADER_HASH,
            **semantic,
        }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "cpu",
            "error": f"{type(exc).__name__}: {exc}",
        }
