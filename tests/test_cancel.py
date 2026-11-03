from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from smartcut.cancel import CancellationToken
from smartcut.config import Settings
from smartcut.errors import RunCancelledError
from smartcut.llm.client import DeepSeekClient
from smartcut.runner.execute import ProcessRunner
from smartcut.runner.paths import Workspace
from smartcut.runner.process_tree import terminate_process_tree
from smartcut.server.db import Database, Project
from smartcut.server.jobs import JobService


def test_job_cancel_sets_cancelled_status(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    db.init()
    with db.session() as session:
        session.add(Project(id="p_cancel", name="cancel", workspace_dir=str(tmp_path)))
    jobs = JobService(db, 1)

    def handler(reporter):
        for _ in range(200):
            reporter.cancel_token.raise_if_cancelled()
            time.sleep(0.01)
        return {"finished": True}

    try:
        job_id = jobs.submit("p_cancel", "slow", handler)
        time.sleep(0.05)
        assert jobs.cancel(job_id) is True
        deadline = time.time() + 5
        job = None
        while time.time() < deadline:
            job = jobs.get(job_id)
            if job is not None and job["status"] in {"cancelled", "failed", "succeeded"}:
                break
            time.sleep(0.02)
        assert job is not None
        assert job["status"] == "cancelled", job
    finally:
        jobs.shutdown(wait=True)
        db.dispose()


class _DummyResponse(BaseModel):
    value: int = 0


def test_llm_request_is_not_sent_after_cancel() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("不应发送请求")

    settings = Settings(
        deepseek_api_key="test-key",
        deepseek_base_url="https://example.invalid",
    )
    client = DeepSeekClient(
        settings,
        transport=httpx.MockTransport(handler),
    )
    token = CancellationToken()
    token.cancel()
    try:
        with pytest.raises(RunCancelledError):
            client.chat_json(
                [{"role": "user", "content": "hi"}],
                _DummyResponse,
                cancel_token=token,
            )
        assert calls == []
    finally:
        client.close()


def test_llm_retry_backoff_aborts_immediately_on_cancel() -> None:
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(503, headers={"Retry-After": "5"})

    settings = Settings(
        deepseek_api_key="test-key",
        deepseek_base_url="https://example.invalid",
        deepseek_max_retries=2,
        deepseek_retry_base_seconds=0.0,
    )
    client = DeepSeekClient(
        settings,
        transport=httpx.MockTransport(handler),
    )
    token = CancellationToken()
    timer = threading.Timer(0.05, token.cancel)
    started = time.monotonic()
    try:
        timer.start()
        with pytest.raises(RunCancelledError):
            client.chat_text(
                [{"role": "user", "content": "retry"}],
                cancel_token=token,
            )
        assert time.monotonic() - started < 0.5
        assert calls["count"] == 1
    finally:
        timer.cancel()
        client.close()


def test_llm_single_flight_waiter_can_be_cancelled() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        time.sleep(0.5)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "late"}}]},
        )

    settings = Settings(
        deepseek_api_key="test-key",
        deepseek_base_url="https://example.invalid",
        deepseek_max_retries=0,
    )
    client = DeepSeekClient(
        settings,
        transport=httpx.MockTransport(handler),
    )
    owner_result: list[str] = []

    def owner() -> None:
        owner_result.append(
            client.chat_text([{"role": "user", "content": "same"}])
        )

    owner_thread = threading.Thread(target=owner)
    token = CancellationToken()
    timer = threading.Timer(0.05, token.cancel)
    try:
        owner_thread.start()
        time.sleep(0.05)
        timer.start()
        started = time.monotonic()
        with pytest.raises(RunCancelledError):
            client.chat_text(
                [{"role": "user", "content": "same"}],
                cancel_token=token,
            )
        assert time.monotonic() - started < 0.45
        owner_thread.join(timeout=2)
        assert owner_result == ["late"]
    finally:
        timer.cancel()
        owner_thread.join(timeout=2)
        client.close()


def test_process_runner_kills_ffmpeg_on_cancel(
    settings: Settings,
    workspace: Workspace,
) -> None:
    token = CancellationToken()
    timer = threading.Timer(0.3, token.cancel)
    runner = ProcessRunner(settings, workspace)
    timer.start()
    try:
        with pytest.raises(RunCancelledError):
            runner.run(
                [
                    settings.ffmpeg_bin,
                    "-hide_banner",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=640x480:rate=30:duration=60",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-f",
                    "null",
                    "-",
                ],
                timeout=60,
                tool="cancel-test",
                cancel_token=token,
            )
    finally:
        timer.cancel()


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return True  # 无法判断时按存活处理，避免测试误报通过
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_process_tree_termination_kills_grandchild(tmp_path: Path) -> None:
    """子孙进程继承 stdout/stderr 时，父进程和孙进程都必须被回收。"""
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "    stdout=sys.stdout,\n"
        "    stderr=sys.stderr,\n"
        ")\n"
        "with open(sys.argv[1], 'w') as handle:\n"
        "    handle.write(str(child.pid))\n"
        "    handle.flush()\n"
        "time.sleep(60)\n"
    )
    popen_kwargs = (
        {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(pid_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **popen_kwargs,
    )
    grandchild_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if pid_file.exists() and pid_file.read_text().strip():
                grandchild_pid = int(pid_file.read_text().strip())
                break
            time.sleep(0.02)
        assert grandchild_pid is not None
        assert _process_alive(grandchild_pid)

        started = time.monotonic()
        terminate_process_tree(proc, timeout=2.0)
        assert time.monotonic() - started < 5.0
        assert proc.poll() is not None

        deadline = time.monotonic() + 3
        while _process_alive(grandchild_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _process_alive(grandchild_pid)
    finally:
        if proc.poll() is None:
            proc.kill()
        if grandchild_pid is not None and _process_alive(grandchild_pid):
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(grandchild_pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
            else:
                os.kill(grandchild_pid, signal.SIGKILL)
