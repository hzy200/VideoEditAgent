"""候选片段面板：FTS 搜索、候选列表与加入时间线。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from smartcut.desktop.widgets.thumbnails import ThumbnailCache

_THUMB_COLUMN = 0
_THUMB_SIZE = (96, 54)
_THUMB_ROW_HEIGHT = 58


class CandidatePanel(QWidget):
    search_requested = Signal(str)
    similar_requested = Signal(str)
    add_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._candidates: list[dict] = []
        self._workspace_root: Path | None = None
        self._thumb_rows: dict[str, list[int]] = {}
        self.thumbs = ThumbnailCache(self, max_items=256)
        self.thumbs.loaded.connect(self._on_thumb_loaded)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("输入关键词检索候选片段；留空显示高分候选")
        self.search_button = QPushButton("搜索")
        row.addWidget(self.query, 1)
        row.addWidget(self.search_button)
        layout.addLayout(row)

        self.status = QLabel("尚未检索候选")
        self.status.setStyleSheet("color:#64748b;")
        layout.addWidget(self.status)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["画面", "ID", "开始(s)", "结束(s)", "分数", "文本", "标签"]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            _THUMB_COLUMN, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(_THUMB_COLUMN, 104)
        self.table.verticalHeader().setDefaultSectionSize(_THUMB_ROW_HEIGHT)
        self.table.verticalHeader().setVisible(False)
        self.table.setIconSize(QSize(*_THUMB_SIZE))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.similar_button = QPushButton("找相似")
        self.add_button = QPushButton("加入时间线")
        self.add_button.setMinimumHeight(32)
        actions.addWidget(self.similar_button)
        actions.addStretch(1)
        actions.addWidget(self.add_button)
        layout.addLayout(actions)

        self.search_button.clicked.connect(self._emit_search)
        self.query.returnPressed.connect(self._emit_search)
        self.similar_button.clicked.connect(self._emit_similar)
        self.add_button.clicked.connect(self._emit_add)
        self.table.cellDoubleClicked.connect(lambda *_: self._emit_add())
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.add_button.setEnabled(False)
        self.similar_button.setEnabled(False)

    def set_workspace_root(self, root: str | Path | None) -> None:
        """设置项目工作区，用于把候选的 keyframe 相对路径解析成缩略图。"""
        self._workspace_root = Path(root) if root else None

    def set_candidates(self, payload: dict | None) -> None:
        payload = payload or {}
        self._candidates = list(payload.get("candidates") or [])
        self._thumb_rows.clear()
        self.table.setRowCount(len(self._candidates))
        for row, candidate in enumerate(self._candidates):
            start, end = _range_seconds(candidate)
            tags = candidate.get("tags") or []
            values = [
                str(candidate.get("id") or ""),
                f"{start:.3f}" if start is not None else "-",
                f"{end:.3f}" if end is not None else "-",
                f"{float(candidate.get('score') or 0):.2f}",
                str(candidate.get("text") or "")[:120],
                ", ".join(str(tag) for tag in tags[:3]),
            ]
            self.table.setItem(row, _THUMB_COLUMN, QTableWidgetItem(""))
            flags = candidate.get("quality_flags") or []
            tooltip_lines = [
                f"分数：{float(candidate.get('score') or 0):.2f}",
                f"标签：{', '.join(str(tag) for tag in tags) or '-'}",
                (
                    f"质量：{', '.join(str(flag) for flag in flags)}"
                    if flags
                    else "质量：无标记"
                ),
                (
                    f"人脸：{'有' if candidate.get('face_present') else '无'}"
                    f" · 镜头边界：{'是' if candidate.get('shot_boundary') else '否'}"
                ),
            ]
            text = str(candidate.get("text") or "")
            if text:
                tooltip_lines.extend(["", text])
            tooltip = "\n".join(tooltip_lines)
            for column, value in enumerate(values, start=_THUMB_COLUMN + 1):
                item = QTableWidgetItem(value)
                item.setToolTip(tooltip)
                self.table.setItem(row, column, item)
            self._request_thumb(row, candidate)
        source = payload.get("source") or "empty"
        self.status.setText(
            f"{len(self._candidates)} 个候选 · 来源 {source}"
            + (f" · 查询 {payload.get('query')!r}" if payload.get("query") else "")
        )
        self.add_button.setEnabled(bool(self._candidates))
        self._sync_buttons()

    def clear(self) -> None:
        self._candidates = []
        self._thumb_rows.clear()
        self.table.setRowCount(0)
        self.status.setText("尚未检索候选")
        self.add_button.setEnabled(False)
        self.similar_button.setEnabled(False)

    def _request_thumb(self, row: int, candidate: dict[str, Any]) -> None:
        keyframe = candidate.get("keyframe")
        if not keyframe or self._workspace_root is None:
            return
        try:
            root = self._workspace_root.resolve()
            path = (root / str(keyframe)).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                return
        except (OSError, ValueError):
            return
        pixmap = self.thumbs.get(str(path), _THUMB_SIZE)
        if pixmap is not None:
            self._apply_thumb(row, pixmap)
            return
        key = self.thumbs.key_for(str(path), _THUMB_SIZE)
        self._thumb_rows.setdefault(key, []).append(row)

    def _on_thumb_loaded(self, key: str, pixmap) -> None:
        rows = self._thumb_rows.pop(key, [])
        for row in rows:
            if 0 <= row < self.table.rowCount():
                self._apply_thumb(row, pixmap)

    def _apply_thumb(self, row: int, pixmap) -> None:
        item = self.table.item(row, _THUMB_COLUMN)
        if item is None:
            item = QTableWidgetItem("")
            self.table.setItem(row, _THUMB_COLUMN, item)
        item.setIcon(QIcon(pixmap))

    def _selected_rows(self) -> list[int]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        return rows

    def _sync_buttons(self) -> None:
        rows = self._selected_rows()
        row = rows[0] if rows else -1
        self.similar_button.setEnabled(
            0 <= row < len(self._candidates)
            and bool(self._candidates[row].get("visual_hash"))
        )

    def _emit_search(self) -> None:
        self.search_requested.emit(self.query.text().strip())

    def _emit_similar(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        row = rows[0]
        if 0 <= row < len(self._candidates):
            candidate_id = str(self._candidates[row].get("id") or "")
            if candidate_id:
                self.similar_requested.emit(candidate_id)

    def _emit_add(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        selected: list[dict] = []
        for row in rows:
            if 0 <= row < len(self._candidates):
                candidate = self._candidates[row]
                start, end = _range_seconds(candidate)
                if start is None or end is None:
                    continue
                selected.append(
                    {
                        "start": start,
                        "end": end,
                        "tags": candidate.get("tags") or [],
                        "text": candidate.get("text") or "",
                        "score": candidate.get("score"),
                    }
                )
        if selected:
            self.add_requested.emit(selected)


def _range_seconds(candidate: dict[str, Any]) -> tuple[float | None, float | None]:
    source_range = candidate.get("source_range") or {}
    start = source_range.get("start") or {}
    end = source_range.get("end") or {}
    try:
        return (
            float(start.get("value", 0)) / float(start.get("rate", 1)),
            float(end.get("value", 0)) / float(end.get("rate", 1)),
        )
    except (TypeError, ValueError, ZeroDivisionError):
        return None, None
