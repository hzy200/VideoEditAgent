from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from smartcut.desktop.formatting import seconds_or_none
from smartcut.desktop.widgets.thumbnails import ThumbnailCache


class PerceptionPanel(QWidget):
    keyframe_selected = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._thumbs = ThumbnailCache(self)
        self._thumb_items: dict[str, list[QListWidgetItem]] = {}
        self._sheet_key: str | None = None
        self._thumbs.loaded.connect(self._on_thumb_ready)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(QLabel("感知摘要"))
        self.summary = QTextBrowser()
        self.summary.setMinimumHeight(180)
        layout.addWidget(self.summary, 1)
        layout.addWidget(QLabel("Contact Sheet"))
        self.sheet = QLabel("暂无 contact sheet")
        self.sheet.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sheet.setMinimumHeight(240)
        self.sheet.setObjectName("mediaCanvas")
        layout.addWidget(self.sheet, 2)
        layout.addWidget(QLabel("章节关键帧（点击跳转预览）"))
        self.keyframes = QListWidget()
        self.keyframes.setViewMode(QListWidget.ViewMode.IconMode)
        self.keyframes.setIconSize(QPixmap(120, 68).size())
        self.keyframes.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.keyframes.setMinimumHeight(110)
        layout.addWidget(self.keyframes, 1)
        self.keyframes.itemClicked.connect(self._on_keyframe_clicked)

    def set_perception(self, data: dict | None, workspace: Path) -> None:
        if not data:
            self.summary.setPlainText("尚无感知结果")
            self.sheet.setText("暂无 contact sheet")
            self.sheet.setPixmap(QPixmap())
            self.keyframes.clear()
            return

        frames = data.get("frame_manifest") or []
        shots = (data.get("shots") or {}).get("shots") or []
        quality = data.get("quality") or {}
        marks = quality.get("marks") or []
        faces = data.get("faces") or {}
        anchors = faces.get("anchors") or []
        by_kind: dict[str, int] = {}
        for mark in marks:
            kind = str(mark.get("kind", "unknown"))
            by_kind[kind] = by_kind.get(kind, 0) + 1

        lines = [
            f"抽帧：{len(frames)} 帧（1fps 采样）",
            f"镜头切点：{len(shots)} 个",
            f"质量标记：{sum(by_kind.values())} 个",
        ]
        if by_kind:
            lines.append(
                "  " + "，".join(f"{kind} {count}" for kind, count in sorted(by_kind.items()))
            )
        lines.append(f"人脸帧：{len(anchors)} 个")
        manifest = data.get("video_manifest") or {}
        if manifest:
            transcript = manifest.get("transcript") or {}
            candidates = manifest.get("candidates") or {}
            cache_keys = manifest.get("cache_keys") or {}
            lines.append(
                "结构化清单："
                f"章节 {len((manifest.get('chapters') or {}).get('chapters') or [])} · "
                f"转写 {int(transcript.get('segment_count') or 0)} 句 · "
                f"候选 {int(candidates.get('count') or 0)} · "
                f"VLM notes {len(manifest.get('visual_notes') or [])} · "
                f"阶段缓存 {len(cache_keys)}"
            )
        warnings = data.get("warnings") or []
        if warnings:
            lines.append("")
            lines.append("提示：")
            lines.extend(f"  - {warning}" for warning in warnings)
        self.summary.setPlainText("\n".join(lines))
        self._set_keyframes(data.get("chapters") or [], workspace)

        contact = data.get("contact_sheet")
        if not contact:
            self._sheet_key = None
            self.sheet.setPixmap(QPixmap())
            self.sheet.setText("暂无 contact sheet")
            return
        path = workspace / contact
        if not path.exists():
            self.sheet.setPixmap(QPixmap())
            self.sheet.setText("contact sheet 文件不存在")
            return
        self._sheet_key = ThumbnailCache.key_for(str(path), (760, 760))
        pixmap = self._thumbs.get(str(path), (760, 760))
        if pixmap is None:
            self.sheet.setPixmap(QPixmap())
            self.sheet.setText("contact sheet 加载中…")
            return
        self.sheet.setText("")
        self.sheet.setPixmap(pixmap)

    def _set_keyframes(self, chapters: list[dict], workspace: Path) -> None:
        self.keyframes.clear()
        self._thumb_items.clear()
        count = 0
        for chapter in chapters:
            start = seconds_or_none(chapter.get("start"))
            for relative in chapter.get("keyframes") or []:
                if count >= 24:
                    return
                item = QListWidgetItem(
                    f"{chapter.get('title') or chapter.get('id')}\n{start or 0:.1f}s"
                )
                item.setData(Qt.ItemDataRole.UserRole, start or 0.0)
                path = workspace / str(relative)
                key = ThumbnailCache.key_for(str(path), (120, 68))
                pixmap = self._thumbs.get(str(path), (120, 68))
                if pixmap is not None:
                    item.setIcon(QIcon(pixmap))
                elif path.exists():
                    self._thumb_items.setdefault(key, []).append(item)
                self.keyframes.addItem(item)
                count += 1

    def _on_thumb_ready(self, key: str, pixmap) -> None:
        if key == self._sheet_key:
            self.sheet.setText("")
            self.sheet.setPixmap(pixmap)
            return
        for item in self._thumb_items.pop(key, []):
            item.setIcon(QIcon(pixmap))

    def _on_keyframe_clicked(self, item: QListWidgetItem) -> None:
        value = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(value, (int, float)):
            self.keyframe_selected.emit(float(value))


