"""手动字幕编辑：输出时间线上的 cue 增删改与实时预览。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


class SubtitlePanel(QWidget):
    cues_changed = Signal(object)
    add_requested = Signal()
    reload_requested = Signal()
    burn_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False
        self._last_valid: list[dict[str, Any]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        hint = QLabel("字幕时间为输出时间线（秒）；编辑后实时预览，保存新版本后写入 EditIR。")
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        actions = QHBoxLayout()
        self.add_button = QPushButton("在播放头添加")
        self.delete_button = QPushButton("删除选中")
        self.reload_button = QPushButton("重新载入字幕")
        self.clear_button = QPushButton("清空")
        for button in (
            self.add_button,
            self.delete_button,
            self.reload_button,
            self.clear_button,
        ):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["开始(s)", "结束(s)", "文本"])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        layout.addWidget(self.table, 1)

        footer = QHBoxLayout()
        self.burn_checkbox = QCheckBox("渲染时烧录字幕")
        self.status_label = QLabel("尚无字幕")
        self.status_label.setObjectName("mutedText")
        footer.addWidget(self.burn_checkbox)
        footer.addStretch(1)
        footer.addWidget(self.status_label)
        layout.addLayout(footer)

        self.add_button.clicked.connect(self.add_requested.emit)
        self.delete_button.clicked.connect(self.remove_selected)
        self.reload_button.clicked.connect(self.reload_requested.emit)
        self.clear_button.clicked.connect(self.clear_cues)
        self.table.itemChanged.connect(self._on_item_changed)
        self.burn_checkbox.toggled.connect(self.burn_changed.emit)
        self._update_status()

    def set_cues(self, cues: list[dict] | None, *, burn: bool | None = None) -> None:
        rows = list(cues or [])
        self._loading = True
        try:
            self.table.setRowCount(len(rows))
            for row, cue in enumerate(rows):
                values = (
                    f"{float(cue.get('start') or 0):.3f}",
                    f"{float(cue.get('end') or 0):.3f}",
                    str(cue.get("text") or ""),
                )
                for column, value in enumerate(values):
                    self.table.setItem(row, column, QTableWidgetItem(value))
            if burn is not None:
                self.burn_checkbox.blockSignals(True)
                self.burn_checkbox.setChecked(bool(burn))
                self.burn_checkbox.blockSignals(False)
        finally:
            self._loading = False
        self._last_valid = rows
        self._update_status()

    def cues(self) -> list[dict[str, Any]]:
        cues, errors = self._collect()
        return [] if errors else cues

    def errors(self) -> list[str]:
        return self._collect()[1]

    def set_burn(self, burn: bool) -> None:
        self.burn_checkbox.blockSignals(True)
        self.burn_checkbox.setChecked(bool(burn))
        self.burn_checkbox.blockSignals(False)

    def burn(self) -> bool:
        return self.burn_checkbox.isChecked()

    def add_cue(self, start: float, end: float, text: str = "新字幕") -> None:
        start = max(0.0, float(start))
        end = max(start + 0.2, float(end))
        self._loading = True
        try:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(f"{start:.3f}"))
            self.table.setItem(row, 1, QTableWidgetItem(f"{end:.3f}"))
            self.table.setItem(row, 2, QTableWidgetItem(text))
            self.table.setCurrentCell(row, 2)
        finally:
            self._loading = False
        self._emit_current()

    def remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        self._loading = True
        try:
            for row in rows:
                self.table.removeRow(row)
        finally:
            self._loading = False
        self._emit_current()

    def clear_cues(self) -> None:
        self._loading = True
        try:
            self.table.setRowCount(0)
        finally:
            self._loading = False
        self._emit_current()

    def _on_item_changed(self, _item: QTableWidgetItem) -> None:
        if self._loading:
            return
        self._emit_current()

    def _emit_current(self) -> None:
        cues, errors = self._collect()
        if errors:
            self.status_label.setText(errors[0])
            self.status_label.setStyleSheet("color:#f87171;")
            return
        self._last_valid = cues
        self._update_status()
        self.cues_changed.emit(cues)

    def _update_status(self) -> None:
        count = len(self._last_valid)
        self.status_label.setStyleSheet("")
        self.status_label.setText(f"{count} 条字幕" if count else "尚无字幕")

    def _collect(self) -> tuple[list[dict[str, Any]], list[str]]:
        cues: list[dict[str, Any]] = []
        errors: list[str] = []
        for row in range(self.table.rowCount()):
            start_item = self.table.item(row, 0)
            end_item = self.table.item(row, 1)
            text_item = self.table.item(row, 2)
            if start_item is None or end_item is None or text_item is None:
                errors.append(f"第 {row + 1} 行缺少字段")
                continue
            try:
                start = float(start_item.text())
                end = float(end_item.text())
            except (TypeError, ValueError):
                errors.append(f"第 {row + 1} 行时间不是数字")
                continue
            text = text_item.text().strip()
            if start < 0:
                errors.append(f"第 {row + 1} 行开始时间不能为负")
                continue
            if end <= start:
                errors.append(f"第 {row + 1} 行结束时间必须大于开始时间")
                continue
            if not text:
                errors.append(f"第 {row + 1} 行文本不能为空")
                continue
            cues.append({"start": start, "end": end, "text": text})
        return cues, errors
