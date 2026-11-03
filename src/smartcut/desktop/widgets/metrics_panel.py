"""运行指标面板：agent 耗时、队列等待、缓存、token 与编码器。"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


class MetricsPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.summary = QLabel("尚无运行指标")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            [
                "Agent",
                "Task",
                "状态",
                "队列(ms)",
                "耗时(ms)",
                "重试",
                "缓存 H/M",
                "Tokens",
                "编码器",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)

    def clear(self) -> None:
        self.summary.setText("尚无运行指标")
        self.table.setRowCount(0)

    def set_run(self, run: dict | None) -> None:
        metrics = (run or {}).get("metrics") or {}
        totals = metrics.get("totals") or {}
        encoders = metrics.get("encoders") or []
        self.summary.setText(
            "总耗时 {wall:.2f}s · Agent 耗时 {duration:.2f}s · 队列等待 {queue:.2f}s · "
            "重试 {retries} · 缓存命中率 {rate:.1%} · LLM 请求 {requests} · "
            "Tokens {tokens} · 编码器 {encoders}".format(
                wall=float(metrics.get("wall_duration_ms") or 0) / 1000.0,
                duration=float(totals.get("duration_ms") or 0) / 1000.0,
                queue=float(totals.get("queue_wait_ms") or 0) / 1000.0,
                retries=int(totals.get("retries") or 0),
                rate=float(totals.get("cache_hit_rate") or 0.0),
                requests=int(totals.get("llm_requests") or 0),
                tokens=int(totals.get("total_tokens") or 0),
                encoders=", ".join(encoders) if encoders else "-",
            )
        )
        tasks = metrics.get("tasks") or []
        self.table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            values = [
                str(task.get("agent") or ""),
                str(task.get("task_id") or ""),
                str(task.get("status") or ""),
                str(task.get("queue_wait_ms") or 0),
                str(task.get("duration_ms") or 0),
                str(task.get("retries") or 0),
                f"{task.get('cache_hits', 0)}/{task.get('cache_misses', 0)}",
                str(task.get("total_tokens") or 0),
                str(task.get("encoder") or "-"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 2:
                    if value == "succeeded":
                        item.setForeground(QColor("#16a34a"))
                    elif value == "failed":
                        item.setForeground(QColor("#dc2626"))
                self.table.setItem(row, column, item)
