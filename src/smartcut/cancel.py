"""跨线程/子进程的取消令牌。"""

from __future__ import annotations

import threading

from smartcut.errors import RunCancelledError


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise RunCancelledError("任务已取消")
