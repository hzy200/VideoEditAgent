"""GPU 解码探测。

只通过 ProcessRunner 执行 ffmpeg 探针；结果按 ffmpeg 二进制签名缓存。
探测失败/运行时失败都回退 CPU，不阻断抽帧或代理生成。
"""

from __future__ import annotations

import threading
from pathlib import Path

from smartcut.config import Settings
from smartcut.errors import ToolExecutionError


def _binary_signature(path: str) -> tuple[str, int, int]:
    try:
        stat = Path(path).stat()
        return (path, stat.st_size, stat.st_mtime_ns)
    except OSError:
        return (path, 0, 0)


class HardwareDecodeSelector:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._available: dict[tuple, bool] = {}

    @property
    def enabled(self) -> bool:
        return str(self.settings.hardware_decoding).lower() not in {
            "off",
            "false",
            "0",
            "no",
        }

    @property
    def backend(self) -> str:
        return str(self.settings.decode_hwaccel or "cuda").lower()

    def available(self, process, sample_src: str) -> bool:
        if not self.enabled:
            return False
        key = (_binary_signature(self.settings.ffmpeg_bin), self.backend)
        with self._lock:
            cached = self._available.get(key)
        if cached is not None:
            return cached
        argv = [
            self.settings.ffmpeg_bin,
            "-hide_banner",
            "-v",
            "error",
            "-hwaccel",
            self.backend,
            "-i",
            sample_src,
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ]
        try:
            process.run(argv, timeout=15.0, tool="decode_probe")
            available = True
        except (ToolExecutionError, OSError):
            available = False
        with self._lock:
            self._available[key] = available
        return available

    def disable(self) -> None:
        """运行时失败后立即关闭当前后端，避免每个任务重复失败。"""
        key = (_binary_signature(self.settings.ffmpeg_bin), self.backend)
        with self._lock:
            self._available[key] = False

    def reset(self) -> None:
        with self._lock:
            self._available.clear()
