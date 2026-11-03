"""跨 Job/Agent 共享的资源池与并发上限。

使用带优先级的公平排队而不是裸 Semaphore：
- 先到先得，避免长任务持续抢到配额；
- 支持等待超时与阶段 deadline，避免无限期阻塞；
- 所有子进程/CPU/LLM/进程池调用都经过同一组配额。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from smartcut.cancel import CancellationToken
from smartcut.config import Settings
from smartcut.errors import ResourceTimeout

# 即使调用方没有显式设置 timeout，也不允许无限期等待配额。
# 1800s 与默认 demucs_timeout 对齐：足够长任务排队，同时避免
# 一个泄漏的槽位把整个进程永久挂住。ResourceManager 会按实际
# 工具超时取更大的值，避免排队时间短于执行预算。
DEFAULT_MAX_WAIT_SECONDS = 1800.0
_CANCEL_POLL_SECONDS = 0.2


@dataclass
class _Waiter:
    priority: int
    sequence: int
    granted: bool = False


class FairLimiter:
    """公平、可超时的并发限制器。priority 越小越先获得配额。"""

    def __init__(self, limit: int, name: str) -> None:
        self.limit = max(1, limit)
        self.name = name
        self.active = 0
        self._waiters: list[_Waiter] = []
        self._sequence = 0
        self._condition = threading.Condition()
        self._stats = {
            "acquired": 0,
            "waited": 0,
            "timeouts": 0,
            "max_wait_ms": 0,
        }

    def _remaining(self, started: float, timeout: float | None, deadline: float | None):
        values: list[float] = []
        if timeout is not None:
            values.append(started + timeout)
        if deadline is not None:
            values.append(deadline)
        if not values:
            return None
        return max(0.0, min(values) - time.perf_counter())

    def acquire(
        self,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
        priority: int = 0,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        started = time.perf_counter()
        # 没有显式 timeout/deadline 时使用安全阀，避免 wait(None) 永久阻塞。
        if timeout is None and deadline is None:
            timeout = DEFAULT_MAX_WAIT_SECONDS
        with self._condition:
            if self.active < self.limit and not self._waiters:
                self.active += 1
                self._stats["acquired"] += 1
                return

            self._sequence += 1
            waiter = _Waiter(priority=int(priority), sequence=self._sequence)
            self._waiters.append(waiter)
            self._waiters.sort(key=lambda item: (item.priority, item.sequence))
            self._stats["waited"] += 1
            try:
                while not waiter.granted:
                    if cancel_token is not None:
                        cancel_token.raise_if_cancelled()
                    remaining = self._remaining(started, timeout, deadline)
                    if remaining is not None and remaining <= 0:
                        self._waiters.remove(waiter)
                        self._stats["timeouts"] += 1
                        self._condition.notify_all()
                        raise ResourceTimeout(
                            f"{self.name} 资源等待超时"
                        )
                    # 有 cancel_token 时短轮询，保证取消最多延迟一个 poll 周期。
                    wait_for = remaining
                    if cancel_token is not None:
                        wait_for = min(
                            _CANCEL_POLL_SECONDS,
                            remaining if remaining is not None else _CANCEL_POLL_SECONDS,
                        )
                    self._condition.wait(wait_for)
            except BaseException:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                    self._condition.notify_all()
                raise
            wait_ms = int((time.perf_counter() - started) * 1000)
            self._stats["max_wait_ms"] = max(
                self._stats["max_wait_ms"],
                wait_ms,
            )
            self._stats["acquired"] += 1

    def release(self) -> None:
        with self._condition:
            if self.active > 0:
                self.active -= 1
            while self.active < self.limit and self._waiters:
                waiter = self._waiters.pop(0)
                waiter.granted = True
                self.active += 1
            self._condition.notify_all()

    @contextmanager
    def slot(
        self,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
        priority: int = 0,
        cancel_token: CancellationToken | None = None,
    ):
        self.acquire(
            timeout=timeout,
            deadline=deadline,
            priority=priority,
            cancel_token=cancel_token,
        )
        try:
            yield
        finally:
            self.release()

    def snapshot(self) -> dict[str, int]:
        with self._condition:
            return {
                **self._stats,
                "active": self.active,
                "waiting": len(self._waiters),
                "limit": self.limit,
            }


class ResourceManager:
    def __init__(
        self,
        settings: Settings,
        *,
        default_timeout: float | None = None,
    ) -> None:
        configured = float(
            getattr(settings, "resource_wait_timeout_seconds", 0.0) or 0.0
        )
        if default_timeout is not None:
            self.default_timeout: float | None = default_timeout
        elif configured > 0:
            self.default_timeout = configured
        else:
            # 0=自动：至少覆盖最长工具超时，否则 Demucs 会在排队阶段
            # 先于自己的执行预算被 ResourceTimeout 打断。
            self.default_timeout = max(
                DEFAULT_MAX_WAIT_SECONDS,
                float(getattr(settings, "render_timeout_seconds", 0.0) or 0.0),
                float(getattr(settings, "demucs_timeout_seconds", 0.0) or 0.0),
            )
        self.ffmpeg = FairLimiter(settings.ffmpeg_concurrency, "ffmpeg")
        self.cpu = FairLimiter(settings.cpu_concurrency, "cpu")
        self.llm = FairLimiter(settings.llm_concurrency, "llm")

    @contextmanager
    def ffmpeg_slot(
        self,
        *,
        timeout=None,
        deadline=None,
        priority=0,
        cancel_token: CancellationToken | None = None,
    ):
        with self.ffmpeg.slot(
            timeout=timeout if timeout is not None else self.default_timeout,
            deadline=deadline,
            priority=priority,
            cancel_token=cancel_token,
        ):
            yield

    @contextmanager
    def cpu_slot(
        self,
        *,
        timeout=None,
        deadline=None,
        priority=0,
        cancel_token: CancellationToken | None = None,
    ):
        with self.cpu.slot(
            timeout=timeout if timeout is not None else self.default_timeout,
            deadline=deadline,
            priority=priority,
            cancel_token=cancel_token,
        ):
            yield

    @contextmanager
    def cpu_slots(
        self,
        count: int,
        *,
        timeout=None,
        deadline=None,
        priority=0,
        cancel_token: CancellationToken | None = None,
    ):
        """一次持有多个 CPU 配额（供进程池整体限流）。"""
        count = max(1, int(count))
        acquired = 0
        try:
            for _ in range(count):
                self.cpu.acquire(
                    timeout=timeout if timeout is not None else self.default_timeout,
                    deadline=deadline,
                    priority=priority,
                    cancel_token=cancel_token,
                )
                acquired += 1
            yield
        finally:
            for _ in range(acquired):
                self.cpu.release()

    @contextmanager
    def llm_slot(
        self,
        *,
        timeout=None,
        deadline=None,
        priority=0,
        cancel_token: CancellationToken | None = None,
    ):
        with self.llm.slot(
            timeout=timeout if timeout is not None else self.default_timeout,
            deadline=deadline,
            priority=priority,
            cancel_token=cancel_token,
        ):
            yield

    def snapshot(self) -> dict[str, dict[str, int]]:
        return {
            "ffmpeg": self.ffmpeg.snapshot(),
            "cpu": self.cpu.snapshot(),
            "llm": self.llm.snapshot(),
        }
