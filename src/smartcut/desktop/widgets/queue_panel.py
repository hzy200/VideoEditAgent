"""任务队列：查看排队/运行任务并批量取消。"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Signal


class QueuePanel(QWidget):
    cancel_requested = Signal(object)
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._jobs: list[dict] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.summary = QLabel("当前没有任务")
        layout.addWidget(self.summary)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Job ID", "类型", "状态", "进度", "说明", "创建时间"]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.refresh_button = QPushButton("刷新队列")
        self.cancel_button = QPushButton("取消选中任务")
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.cancel_button.clicked.connect(self._emit_cancel)
        self.cancel_button.setEnabled(False)

    def set_jobs(self, payload: dict | None) -> None:
        payload = payload or {}
        self._jobs = list(payload.get("jobs") or [])
        active = sum(
            1 for job in self._jobs if job.get("status") in {"queued", "running"}
        )
        self.summary.setText(
            f"{len(self._jobs)} 个任务 · {active} 个排队/运行中"
        )
        self.table.setRowCount(len(self._jobs))
        for row, job in enumerate(self._jobs):
            values = [
                str(job.get("id") or ""),
                str(job.get("kind") or ""),
                str(job.get("status") or ""),
                f"{int(job.get('progress') or 0)}%",
                str(job.get("message") or job.get("error") or ""),
                str(job.get("created_at") or ""),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 2:
                    status = value
                    if status == "succeeded":
                        item.setForeground(QColor("#16a34a"))
                    elif status == "failed":
                        item.setForeground(QColor("#dc2626"))
                    elif status in {"queued", "running"}:
                        item.setForeground(QColor("#d97706"))
                self.table.setItem(row, column, item)
        self.cancel_button.setEnabled(bool(self._jobs))

    def _emit_cancel(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        job_ids = [
            str(self._jobs[row]["id"])
            for row in rows
            if 0 <= row < len(self._jobs)
        ]
        if job_ids:
            self.cancel_requested.emit(job_ids)
