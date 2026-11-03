from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt, Signal


class RunsPanel(QWidget):
    run_selected = Signal(str)
    resume_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runs: dict[str, dict] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        row = QVBoxLayout()
        row.addWidget(QLabel("双击运行记录查看最终答案与账本"))
        self.resume_button = QPushButton("续跑选中运行（缓存辅助）")
        self.resume_button.clicked.connect(self._emit_resume)
        row.addWidget(self.resume_button)
        layout.addLayout(row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["时间", "状态", "目标", "置信度", "Run ID"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.table, 1)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.answer = QTextBrowser()
        splitter.addWidget(self.answer)

        self.ledger = QTableWidget(0, 4)
        self.ledger.setHorizontalHeaderLabels(["任务", "Agent", "状态", "说明"])
        self.ledger.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.ledger.verticalHeader().setVisible(False)
        splitter.addWidget(self.ledger)
        splitter.setSizes([180, 200])
        layout.addWidget(splitter, 1)

    def set_runs(self, runs: list[dict]) -> None:
        self._runs = {run["id"]: run for run in runs}
        self.table.setRowCount(0)
        for run in runs:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                run.get("created_at") or "",
                run.get("status") or "",
                run.get("goal") or "",
                f"{float(run.get('confidence') or 0):.2f}",
                run.get("id") or "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 1:
                    status = run.get("status")
                    if status == "succeeded":
                        item.setForeground(QColor("#16a34a"))
                    elif status == "failed":
                        item.setForeground(QColor("#dc2626"))
                    elif status == "partial":
                        item.setForeground(QColor("#d97706"))
                self.table.setItem(row, column, item)

    def show_run(self, run: dict) -> None:
        self.answer.setPlainText(run.get("answer") or "")
        self.ledger.setRowCount(0)
        for outcome in run.get("ledger") or []:
            row = self.ledger.rowCount()
            self.ledger.insertRow(row)
            values = [
                outcome.get("task_id", ""),
                outcome.get("agent", ""),
                outcome.get("status", ""),
                outcome.get("summary") or outcome.get("error") or "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 2:
                    if value == "succeeded":
                        item.setForeground(QColor("#16a34a"))
                    elif value == "failed":
                        item.setForeground(QColor("#dc2626"))
                    elif value == "skipped":
                        item.setForeground(QColor("#d97706"))
                self.ledger.setItem(row, column, item)

    def _selected_run_id(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 4)
        return item.text() if item is not None else None

    def _emit_resume(self) -> None:
        run_id = self._selected_run_id()
        if run_id:
            self.resume_requested.emit(run_id)

    def _on_double_click(self, row: int, _column: int) -> None:
        item = self.table.item(row, 4)
        if item is None:
            return
        run_id = item.text()
        run = self._runs.get(run_id)
        if run is not None:
            self.show_run(run)
            self.run_selected.emit(run_id)
