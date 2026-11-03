from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import smartcut.resources as resources_module
from smartcut.cancel import CancellationToken
from smartcut.config import Settings
from smartcut.errors import ResourceTimeout, RunCancelledError
from smartcut.resources import FairLimiter, ResourceManager
from smartcut.runner.execute import ProcessRunner
from smartcut.runner.paths import Workspace


def _wait_waiting(limiter: FairLimiter, expected: int, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if limiter.snapshot()["waiting"] >= expected:
            return
        time.sleep(0.01)
    raise AssertionError("等待者未进入队列")


def test_fair_limiter_prefers_higher_priority() -> None:
    limiter = FairLimiter(1, "test")
    order: list[str] = []
    limiter.acquire()

    def worker(name: str, priority: int) -> None:
        limiter.acquire(priority=priority)
        order.append(name)
        time.sleep(0.02)
        limiter.release()

    low = threading.Thread(target=worker, args=("low", 10))
    high = threading.Thread(target=worker, args=("high", 1))
    low.start()
    _wait_waiting(limiter, 1)
    high.start()
    _wait_waiting(limiter, 2)
    limiter.release()
    low.join(2.0)
    high.join(2.0)
    assert order == ["high", "low"]


def test_fair_limiter_timeout_and_snapshot() -> None:
    limiter = FairLimiter(1, "cpu")
    limiter.acquire()
    with pytest.raises(ResourceTimeout):
        limiter.acquire(timeout=0.05)
    with pytest.raises(ResourceTimeout):
        limiter.acquire(deadline=time.perf_counter() + 0.05)
    limiter.release()
    snapshot = limiter.snapshot()
    assert snapshot["timeouts"] == 2
    assert snapshot["active"] == 0


def test_resource_manager_exposes_slots_and_metrics() -> None:
    settings = Settings(
        workspace_root=Path(__file__).parent,
        ffmpeg_concurrency=1,
        cpu_concurrency=2,
        llm_concurrency=1,
        resource_wait_timeout_seconds=1.0,
    )
    manager = ResourceManager(settings)
    with manager.ffmpeg_slot():
        with pytest.raises(ResourceTimeout):
            with manager.ffmpeg_slot(timeout=0.05):
                pass
    with manager.cpu_slots(2):
        snapshot = manager.snapshot()
        assert snapshot["cpu"]["active"] == 2
    assert manager.snapshot()["cpu"]["active"] == 0


def test_fair_limiter_wait_is_cancellable() -> None:
    limiter = FairLimiter(1, "cancel")
    limiter.acquire()
    token = CancellationToken()
    timer = threading.Timer(0.05, token.cancel)
    started = time.monotonic()
    try:
        timer.start()
        with pytest.raises(RunCancelledError):
            limiter.acquire(cancel_token=token)
        assert time.monotonic() - started < 1.0
        assert limiter.snapshot()["waiting"] == 0
    finally:
        timer.cancel()
        limiter.release()


def test_fair_limiter_has_default_wait_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resources_module, "DEFAULT_MAX_WAIT_SECONDS", 0.05)
    limiter = FairLimiter(1, "ceiling")
    limiter.acquire()
    try:
        with pytest.raises(ResourceTimeout):
            limiter.acquire()
    finally:
        limiter.release()


def test_resource_manager_auto_wait_ceiling_covers_tool_timeouts() -> None:
    settings = Settings(
        workspace_root=Path(__file__).parent,
        resource_wait_timeout_seconds=0.0,
    )
    manager = ResourceManager(settings)
    assert manager.default_timeout is not None
    assert manager.default_timeout >= settings.demucs_timeout_seconds
    assert manager.default_timeout >= settings.render_timeout_seconds


def test_resource_manager_slot_passes_cancel_token() -> None:
    settings = Settings(
        workspace_root=Path(__file__).parent,
        cpu_concurrency=1,
        resource_wait_timeout_seconds=5.0,
    )
    manager = ResourceManager(settings)
    token = CancellationToken()
    token.cancel()
    with manager.cpu_slot():
        with pytest.raises(RunCancelledError):
            with manager.cpu_slot(cancel_token=token):
                pass
    assert manager.snapshot()["cpu"]["waiting"] == 0


class _SlowSlotManager:
    """模拟资源排队：ffmpeg_slot 先阻塞一段时间再放行。"""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    @contextmanager
    def ffmpeg_slot(self, **_kwargs):
        time.sleep(self.delay)
        yield


class _FakeProcess:
    """假进程：只验证 deadline 的起点，不引入解释器启动开销。"""

    def __init__(self) -> None:
        self.returncode = 0
        self.killed = False

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        time.sleep(0.05)
        self.returncode = 0
        return "", ""

    def kill(self) -> None:
        self.killed = True

    def poll(self) -> int:
        return self.returncode


def test_process_timeout_starts_after_resource_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        workspace_root=tmp_path,
        ffmpeg_bin=sys.executable,
        ffprobe_bin=sys.executable,
    )
    fake = _FakeProcess()
    monkeypatch.setattr(
        "smartcut.runner.execute.subprocess.Popen",
        lambda *_args, **_kwargs: fake,
    )
    runner = ProcessRunner(
        settings,
        Workspace(tmp_path),
        resources=_SlowSlotManager(0.3),
    )
    result = runner.run(
        [sys.executable, "-c", "pass"],
        timeout=0.2,
        tool="queue-timeout-test",
    )
    assert result.exit_code == 0
    assert fake.killed is False
