"""诊断面板：运行环境摘要与最近日志。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


class DiagnosticsPanel(QWidget):
    refresh_requested = Signal()
    open_log_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.info = QTextBrowser()
        self.info.setMinimumHeight(150)
        self.info.setPlainText("尚未加载诊断信息")
        layout.addWidget(QLabel("环境与运行状态"))
        layout.addWidget(self.info, 1)

        actions = QHBoxLayout()
        self.refresh_button = QPushButton("刷新诊断")
        self.open_button = QPushButton("打开日志目录")
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.open_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        layout.addWidget(QLabel("最近日志"))
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(2000)
        layout.addWidget(self.logs, 2)

        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.open_button.clicked.connect(self.open_log_requested.emit)

    def set_diagnostics(self, payload: dict | None) -> None:
        payload = payload or {}
        info = payload.get("info") or {}
        lines = [f"{key}: {value}" for key, value in info.items()]
        self.info.setPlainText("\n".join(lines) if lines else "暂无诊断信息")
        logs = payload.get("logs") or []
        self.logs.setPlainText("".join(str(line) for line in logs))
        self.logs.verticalScrollBar().setValue(
            self.logs.verticalScrollBar().maximum()
        )
