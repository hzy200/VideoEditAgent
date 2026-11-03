"""QThreadPool 任务封装：工作线程只发信号，不直接操作 UI。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


class TaskSignals(QObject):
    finished = Signal(str, object)
    failed = Signal(str, str)


class Task(QRunnable):
    def __init__(self, op: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.op = op
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = TaskSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:
            self.signals.failed.emit(self.op, f"{type(exc).__name__}: {exc}")
        else:
            self.signals.finished.emit(self.op, result)
