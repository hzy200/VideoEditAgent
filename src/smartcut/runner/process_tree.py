"""子进程树终止与有界回收。

线程无法被 kill；不可取消的同步调用只能落到进程边界。
只杀直接子进程不够：ffmpeg/Demucs 的包装脚本会派生子孙进程并
继承 stdout/stderr 管道。父进程退出后，子孙仍持有管道写端，
``communicate()`` 会永久等待 EOF。本模块统一负责杀进程树，并给
管道回收加上有界超时。
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any

_READ_TIMEOUT_SECONDS = 5.0
_WAIT_TIMEOUT_SECONDS = 1.0


def popen_group_kwargs() -> dict[str, Any]:
    """让子进程进入独立进程组，便于整组终止。

    Windows 使用 CREATE_NO_WINDOW；POSIX 使用 start_new_session，
    这样 killpg 不会误伤当前进程。
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def kill_process_tree(proc: subprocess.Popen) -> None:
    """尽力终止 proc 及其子孙；不保证目标一定消失。"""
    if os.name == "nt":
        _kill_tree_windows(proc)
    else:
        _kill_tree_posix(proc)


def terminate_process_tree(
    proc: subprocess.Popen,
    *,
    timeout: float = _READ_TIMEOUT_SECONDS,
) -> tuple[str, str]:
    """终止进程树并在有界时间内回收管道。

    即使子孙进程仍持有管道写端，也不会无限阻塞调用线程。
    返回已读到的输出；回收失败时返回空串，由调用方按取消/超时处理。
    """
    kill_process_tree(proc)
    try:
        stdout, stderr = proc.communicate(timeout=max(0.1, timeout))
        return stdout or "", stderr or ""
    except (OSError, ValueError, subprocess.TimeoutExpired):
        _close_streams(proc)
        try:
            proc.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        return "", ""


def _kill_tree_windows(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_READ_TIMEOUT_SECONDS,
            check=False,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def _kill_tree_posix(proc: subprocess.Popen) -> None:
    try:
        pgid: int | None = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None
    try:
        own_pgid: int | None = os.getpgid(0)
    except OSError:
        own_pgid = None
    if pgid is not None and pgid != own_pgid:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def _close_streams(proc: subprocess.Popen) -> None:
    for stream in (
        getattr(proc, "stdout", None),
        getattr(proc, "stderr", None),
        getattr(proc, "stdin", None),
    ):
        if stream is None:
            continue
        try:
            stream.close()
        except (OSError, ValueError):
            pass
