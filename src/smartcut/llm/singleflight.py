"""同键并发请求合并（single-flight）。

同一缓存键的并发调用只执行一次，其余调用等待并复用结果/异常。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass
class _Call:
    done: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    error: BaseException | None = None


class SingleFlight:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[Hashable, _Call] = {}

    def run(
        self,
        key: Hashable,
        fn: Callable[[], T],
        *,
        cancel_token: Any | None = None,
    ) -> T:
        with self._lock:
            call = self._calls.get(key)
            owner = call is None
            if owner:
                call = _Call()
                self._calls[key] = call

        if not owner:
            while not call.done.wait(timeout=0.2):
                if cancel_token is not None:
                    cancel_token.raise_if_cancelled()
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            if call.error is not None:
                raise call.error
            return call.value

        try:
            value = fn()
        except BaseException as exc:
            call.error = exc
            raise
        else:
            call.value = value
            return value
        finally:
            call.done.set()
            with self._lock:
                self._calls.pop(key, None)
