"""硬件编码器探测与选择。

探测通过 ProcessRunner 执行 ffmpeg，不在编译器或模块导入时启动进程。
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from smartcut.config import Settings
from smartcut.contracts.encoder import (
    ENCODER_CODECS,
    EncoderProfile,
    video_encoder_args,
)
from smartcut.errors import ToolExecutionError
from smartcut.policy import Policy

_ENCODER_LINE = re.compile(r"^\s*[VAS][A-Z.]{5}\s+(\S+)", re.MULTILINE)


def parse_encoder_list(text: str) -> set[str]:
    return {match.group(1) for match in _ENCODER_LINE.finditer(text or "")}


def profile_from_policy(policy: Policy, codec: str) -> EncoderProfile:
    item = policy.encoding.codecs.get(codec)
    if item is None:
        return EncoderProfile(codec="libx264")
    return EncoderProfile(codec=codec, preset=item.preset, quality=item.quality)


def choose_profile(
    policy: Policy,
    available: set[str],
    priority: list[str],
    *,
    enabled: bool = True,
) -> EncoderProfile:
    if not enabled:
        return profile_from_policy(policy, "libx264")
    for codec in priority:
        if codec == "libx264" or codec in available:
            if codec in ENCODER_CODECS:
                return profile_from_policy(policy, codec)
    return profile_from_policy(policy, "libx264")


def _binary_signature(path: str) -> tuple[str, int, int]:
    try:
        stat = Path(path).stat()
        return (path, stat.st_size, stat.st_mtime_ns)
    except OSError:
        return (path, 0, 0)


class EncoderSelector:
    """进程内缓存探测结果；线程安全。"""

    def __init__(self, settings: Settings, policy: Policy) -> None:
        self.settings = settings
        self.policy = policy
        self._lock = threading.RLock()
        self._available: dict[tuple[str, int, int], set[str]] = {}

    @property
    def enabled(self) -> bool:
        return str(self.settings.hardware_encoding).lower() not in {"off", "false", "0", "no"}

    def detect(self, process: Any) -> set[str]:
        if not self.enabled:
            return {"libx264"}
        key = _binary_signature(self.settings.ffmpeg_bin)
        with self._lock:
            cached = self._available.get(key)
        if cached is not None:
            return cached
        try:
            result = process.run(
                [self.settings.ffmpeg_bin, *self.policy.encoding.probe_args],
                timeout=15.0,
                tool="encoder_probe",
            )
            available = parse_encoder_list(result.stdout + "\n" + result.stderr)
        except (ToolExecutionError, OSError):
            available = set()
        available.add("libx264")
        available &= set(ENCODER_CODECS)
        # ffmpeg 列出编码器 != 驱动可用；对硬件编码器做一次 64x64 试编码。
        available = {
            codec
            for codec in available
            if codec == "libx264" or self._test_encode(process, codec)
        }
        with self._lock:
            self._available[key] = available
        return available

    def _test_encode(self, process: Any, codec: str) -> bool:
        profile = profile_from_policy(self.policy, codec)
        argv = [
            self.settings.ffmpeg_bin,
            "-hide_banner",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:size=64x64:rate=1:duration=0.2",
            "-frames:v",
            "1",
            *video_encoder_args(profile),
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-f",
            "null",
            "-",
        ]
        try:
            process.run(argv, timeout=15.0, tool="encoder_test")
            return True
        except (ToolExecutionError, OSError):
            return False

    def resolve(
        self,
        process: Any,
        *,
        exclude: set[str] | None = None,
    ) -> EncoderProfile:
        available = self.detect(process)
        if exclude:
            available = available - set(exclude)
        priority = self.settings.encoder_priority_list or self.policy.encoding.priority_default
        return choose_profile(
            self.policy,
            available,
            priority,
            enabled=self.enabled,
        )

    def summary(self, process: Any) -> dict:
        available = sorted(self.detect(process))
        return {
            "enabled": self.enabled,
            "available": available,
            "selected": self.resolve(process).codec,
        }
