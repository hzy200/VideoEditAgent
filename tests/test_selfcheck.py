from __future__ import annotations

import ast
import wave
from pathlib import Path

from smartcut.config import Settings
from smartcut.runner.paths import Workspace
from smartcut.runner.selfcheck import (
    probe_binary,
    probe_separator,
    write_test_tone,
)


def test_write_test_tone_without_ffmpeg(tmp_path: Path) -> None:
    path = write_test_tone(tmp_path / "tone.wav")
    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16_000
        assert handle.getnframes() == 8_000


def test_probe_binary_reports_missing_binary(tmp_path: Path) -> None:
    settings = Settings(
        workspace_root=tmp_path / "workspace",
        ffmpeg_bin=str(tmp_path / "missing-ffmpeg.exe"),
        ffprobe_bin=str(tmp_path / "missing-ffprobe.exe"),
    )
    workspace = Workspace(tmp_path / "workspace")
    workspace.root.mkdir(parents=True, exist_ok=True)
    result = probe_binary(
        settings,
        workspace,
        settings.ffmpeg_bin,
        name="ffmpeg",
    )
    assert result["ok"] is False
    assert "missing-ffmpeg" in result["path"]


def test_probe_separator_is_noop_without_command() -> None:
    assert probe_separator(None) == {"ok": True, "available": False}


def test_desktop_app_does_not_start_subprocesses() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "src" / "smartcut" / "desktop" / "app.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "subprocess" not in imported
