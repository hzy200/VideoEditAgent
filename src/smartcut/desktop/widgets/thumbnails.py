"""异步缩略图加载与 LRU 缓存；QImage 在工作线程解码，QPixmap 在 GUI 线程创建。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QImage, QPixmap


class _ThumbSignals(QObject):
    loaded = Signal(str, object)


class _ThumbTask(QRunnable):
    def __init__(self, key: str, path: str, size: tuple[int, int]) -> None:
        super().__init__()
        self.key = key
        self.path = path
        self.size = size
        self.signals = _ThumbSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        image = QImage(self.path)
        if image.isNull():
            self.signals.loaded.emit(self.key, QImage())
            return
        scaled = image.scaled(
            QSize(self.size[0], self.size[1]),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.signals.loaded.emit(self.key, scaled)


class ThumbnailCache(QObject):
    loaded = Signal(str, object)

    def __init__(self, parent: QObject | None = None, *, max_items: int = 256) -> None:
        super().__init__(parent)
        self.max_items = max_items
        self._cache: dict[str, QPixmap] = {}
        self._order: list[str] = []
        self._pending: set[str] = set()
        self._tasks: dict[str, _ThumbTask] = {}
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)

    @staticmethod
    def key_for(path: str, size: tuple[int, int]) -> str:
        return f"{path}|{size[0]}x{size[1]}"

    def get(self, path: str, size: tuple[int, int]) -> QPixmap | None:
        key = self.key_for(path, size)
        cached = self._cache.get(key)
        if cached is not None:
            self._touch(key)
            return cached
        if key not in self._pending and Path(path).is_file():
            self._pending.add(key)
            task = _ThumbTask(key, path, size)
            self._tasks[key] = task
            task.signals.loaded.connect(self._on_loaded)
            self._pool.start(task)
        return None

    @Slot(str, object)
    def _on_loaded(self, key: str, image: QImage) -> None:
        self._pending.discard(key)
        self._tasks.pop(key, None)
        if image is None or image.isNull():
            return
        pixmap = QPixmap.fromImage(image)
        self._cache[key] = pixmap
        self._touch(key)
        self.loaded.emit(key, pixmap)

    def _touch(self, key: str) -> None:
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)
        while len(self._order) > self.max_items:
            old = self._order.pop(0)
            self._cache.pop(old, None)

    def clear(self) -> None:
        self._cache.clear()
        self._order.clear()
        self._pending.clear()
        self._tasks.clear()
