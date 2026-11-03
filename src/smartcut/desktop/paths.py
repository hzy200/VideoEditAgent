"""桌面应用的路径解析：应用数据、内嵌 ffmpeg、内嵌 whisper 模型。"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "SmartCut"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """开发态是项目根目录；PyInstaller 下是 _MEIPASS。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[3]


def app_data_dir(override: str | Path | None = None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    env = os.getenv("SMARTCUT_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    local = os.getenv("LOCALAPPDATA")
    if local:
        return Path(local) / APP_NAME
    return Path.home() / ".smartcut"


def _binary_name(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def bundled_binary(name: str) -> str:
    """优先使用内嵌二进制，其次 PATH（开发态兜底）。"""
    root = resource_root()
    candidates: list[Path] = []
    if is_frozen():
        candidates.append(root / "bin" / _binary_name(name))
    else:
        platform_dir = "win64" if os.name == "nt" else sys.platform
        candidates.append(root / "vendor" / "ffmpeg" / platform_dir / _binary_name(name))
        candidates.append(root / "vendor" / "ffmpeg" / "bin" / _binary_name(name))

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    found = shutil.which(name)
    return found or name


def bundled_separator() -> str | None:
    """伴生分离 worker；仅在打包目录中自动发现。"""
    if not is_frozen():
        return None
    exe_dir = Path(sys.executable).parent
    name = _binary_name("SmartCut-Separator")
    for candidate in (
        exe_dir / name,
        exe_dir / "separator" / name,
    ):
        if candidate.exists():
            return str(candidate)
    return None


def bundled_whisper_dir(model: str = "base") -> Path | None:
    root = resource_root()
    if is_frozen():
        candidate = root / "models" / "whisper" / model
    else:
        candidate = root / "vendor" / "whisper" / model
    return candidate if candidate.exists() else None
