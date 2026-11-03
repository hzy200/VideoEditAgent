"""大纲确认面板：勾选、改名、排序章节，预览关键帧。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from smartcut.contracts.time import RationalTime
from smartcut.desktop.formatting import format_seconds, seconds_or_none
from smartcut.desktop.widgets.thumbnails import ThumbnailCache


class OutlinePanel(QWidget):
    approve_requested = Signal(object)
    refresh_requested = Signal()
    seek_requested = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._payload: dict[str, Any] = {}
        self._workspace = Path(".")
        self._thumbs = ThumbnailCache(self)
        self._thumb_items: dict[str, list[QListWidgetItem]] = {}
        self._thumbs.loaded.connect(self._on_thumb_ready)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.summary = QLabel("尚未生成大纲")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["保留", "标题", "开始", "结束", "关键帧"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.EditKeyPressed
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 56)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 90)
        layout.addWidget(self.table, 3)

        self.details = QTextBrowser()
        self.details.setMinimumHeight(80)
        self.details.setPlainText("选择章节查看摘要与转写上下文")
        layout.addWidget(QLabel("章节摘要 / 转写上下文"))
        layout.addWidget(self.details, 1)

        self.keyframes = QListWidget()
        self.keyframes.setViewMode(QListWidget.ViewMode.IconMode)
        self.keyframes.setIconSize(QPixmap(160, 90).size())
        self.keyframes.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.keyframes.setMinimumHeight(120)
        layout.addWidget(QLabel("章节关键帧（点击可跳转预览）"))
        layout.addWidget(self.keyframes, 2)

        actions = QHBoxLayout()
        self.up_button = QPushButton("上移")
        self.down_button = QPushButton("下移")
        self.refresh_button = QPushButton("刷新大纲")
        self.approve_button = QPushButton("确认大纲并继续")
        self.approve_button.setMinimumHeight(34)
        actions.addWidget(self.up_button)
        actions.addWidget(self.down_button)
        actions.addWidget(self.refresh_button)
        actions.addStretch(1)
        actions.addWidget(self.approve_button)
        layout.addLayout(actions)

        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button.clicked.connect(lambda: self._move(1))
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.approve_button.clicked.connect(self._approve)
        self.table.currentCellChanged.connect(lambda *_: self._show_keyframes())
        self.keyframes.itemClicked.connect(self._emit_seek)
        self.approve_button.setEnabled(False)

    def set_outline(self, payload: dict | None, workspace: Path) -> None:
        self._payload = payload or {}
        self._workspace = workspace
        chapters = self._payload.get("chapters") or []
        status = self._payload.get("status")
        approved = bool(self._payload.get("approved"))
        self.summary.setText(
            f"运行 {self._payload.get('run_id', '-')} · {status or '未知'} · "
            f"{len(chapters)} 个章节"
            + ("（已确认）" if approved else "")
        )
        self.table.setRowCount(0)
        for chapter in chapters:
            row = self.table.rowCount()
            self.table.insertRow(row)
            keep = QTableWidgetItem()
            keep.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            keep.setCheckState(Qt.CheckState.Checked)
            keep.setData(Qt.ItemDataRole.UserRole, chapter)
            self.table.setItem(row, 0, keep)
            title = QTableWidgetItem(str(chapter.get("title") or chapter.get("id") or ""))
            title.setData(Qt.ItemDataRole.UserRole, chapter)
            self.table.setItem(row, 1, title)
            self.table.setItem(row, 2, QTableWidgetItem(format_seconds(chapter.get("start"))))
            self.table.setItem(row, 3, QTableWidgetItem(format_seconds(chapter.get("end"))))
            keyframes = chapter.get("keyframes") or []
            self.table.setItem(row, 4, QTableWidgetItem(str(len(keyframes))))
        if self.table.rowCount():
            self.table.selectRow(0)
        self._show_keyframes()
        self.approve_button.setEnabled(
            bool(chapters) and not approved and status == "awaiting_approval"
        )

    def _move(self, delta: int) -> None:
        row = self.table.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= self.table.rowCount():
            return
        for column in range(self.table.columnCount()):
            item = self.table.takeItem(row, column)
            self.table.setItem(row, column, self.table.takeItem(target, column))
            self.table.setItem(target, column, item)
        self.table.selectRow(target)

    def _show_keyframes(self) -> None:
        self.keyframes.clear()
        self._thumb_items.clear()
        row = self.table.currentRow()
        if row < 0 or row >= self.table.rowCount():
            self.details.setPlainText("选择章节查看摘要与转写上下文")
            return
        item = self.table.item(row, 0)
        chapter = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(chapter, dict):
            return
        start = seconds_or_none(chapter.get("start"))
        self.details.setPlainText(
            "\n".join(
                [
                    f"章节：{chapter.get('title') or chapter.get('id') or '-'}",
                    f"时间：{format_seconds(chapter.get('start'))} – {format_seconds(chapter.get('end'))}",
                    f"镜头：{chapter.get('shot_count', 0)} · "
                    f"质量标记：{chapter.get('quality_marks', 0)} · "
                    f"人脸帧：{chapter.get('face_frames', 0)}",
                    "",
                    str(chapter.get("summary") or "该章节没有转写摘要。"),
                ]
            )
        )
        for index, relative in enumerate(chapter.get("keyframes") or []):
            path = self._workspace / str(relative)
            entry = QListWidgetItem(f"关键帧 {index + 1}")
            entry.setData(Qt.ItemDataRole.UserRole, start)
            entry.setToolTip(str(relative))
            key = ThumbnailCache.key_for(str(path), (160, 90))
            pixmap = self._thumbs.get(str(path), (160, 90))
            if pixmap is not None:
                entry.setIcon(QIcon(pixmap))
            elif path.exists():
                self._thumb_items.setdefault(key, []).append(entry)
            self.keyframes.addItem(entry)
        if not self.keyframes.count():
            self.keyframes.addItem("该章节没有关键帧")

    def _on_thumb_ready(self, key: str, pixmap) -> None:
        for item in self._thumb_items.pop(key, []):
            item.setIcon(QIcon(pixmap))

    def _emit_seek(self, item: QListWidgetItem) -> None:
        value = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(value, (int, float)):
            self.seek_requested.emit(float(value))

    def _edited_time(
        self,
        row: int,
        column: int,
        chapter: dict,
        key: str,
    ) -> dict | None:
        item = self.table.item(row, column)
        original = chapter.get(key)
        text = item.text().strip() if item is not None else ""
        if not text or text == format_seconds(original):
            return original
        try:
            return RationalTime.from_seconds(text, rate=90_000).model_dump()
        except ValueError as exc:
            raise ValueError(
                f"章节 {chapter.get('id')} 的 {key} 无法解析: {text!r}"
            ) from exc

    def _approve(self) -> None:
        selected: list[dict] = []
        try:
            for row in range(self.table.rowCount()):
                keep = self.table.item(row, 0)
                if keep is None or keep.checkState() != Qt.CheckState.Checked:
                    continue
                chapter = keep.data(Qt.ItemDataRole.UserRole)
                if not isinstance(chapter, dict):
                    continue
                title_item = self.table.item(row, 1)
                selected.append(
                    {
                        "id": chapter.get("id"),
                        "title": title_item.text().strip()
                        if title_item
                        else chapter.get("title"),
                        "start": self._edited_time(row, 2, chapter, "start"),
                        "end": self._edited_time(row, 3, chapter, "end"),
                    }
                )
        except ValueError as exc:
            QMessageBox.warning(self, "大纲时间无效", str(exc))
            return
        if selected:
            self.approve_requested.emit(selected)

    def run_id(self) -> str | None:
        value = self._payload.get("run_id")
        return str(value) if value else None

    def clear(self) -> None:
        self._payload = {}
        self.table.setRowCount(0)
        self.keyframes.clear()
        self.summary.setText("尚未生成大纲")
        self.approve_button.setEnabled(False)


