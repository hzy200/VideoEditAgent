"""桌面自检探测：所有子进程调用集中在此，GUI 层不得直接起进程。

ffmpeg/ffprobe 走 ``ProcessRunner``；分离器 worker 是外部适配器，
其 ``--probe`` 由本模块用 ``shell=False`` 启动；测试音频用 stdlib 生成，
不依赖 ffmpeg。
"""

from __future__ import annotations

import json
import math
import struct
import subprocess
import wave
from pathlib import Path
from typing import Any

from smartcut.config import Settings
from smartcut.runner.execute import ProcessRunner
from smartcut.runner.paths import Workspace


def probe_binary(
    settings: Settings,
    workspace: Workspace,
    path: str,
    *,
    name: str,
    timeout: float = 30,
) -> dict[str, Any]:
    """探测 ffmpeg/ffprobe 版本；失败返回结构化错误，不抛出。"""
    runner = ProcessRunner(settings, workspace)
    try:
        result = runner.run([path, "-version"], timeout=timeout, tool=f"selfcheck:{name}")
    except Exception as exc:  # 自检必须收集所有失败
        return {"path": path, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    lines = (result.stdout or result.stderr).strip().splitlines()
    return {
        "path": path,
        "ok": result.exit_code == 0,
        "version": lines[0] if lines else "",
    }


def probe_separator(
    command: list[str] | None,
    *,
    timeout: float = 60,
) -> dict[str, Any]:
    """调用分离器 worker 的 ``--probe``，返回其 JSON 输出。"""
    if not command:
        return {"ok": True, "available": False}
    try:
        proc = subprocess.run(
            [*command, "--probe"],
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        lines = (proc.stdout or "").strip().splitlines()
        detail: dict[str, Any] = (
            json.loads(lines[-1]) if lines else {"returncode": proc.returncode}
        )
        detail["ok"] = proc.returncode == 0
        return detail
    except Exception as exc:  # 自检必须收集所有失败
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def write_test_tone(
    path: Path,
    *,
    seconds: float = 0.5,
    sample_rate: int = 16_000,
    frequency: float = 440.0,
) -> Path:
    """生成 16k 单声道正弦 WAV，供本地转写自检使用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for index in range(int(sample_rate * seconds)):
        value = int(32767 * 0.2 * math.sin(2 * math.pi * frequency * index / sample_rate))
        frames += struct.pack("<h", value)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(frames))
    return path
