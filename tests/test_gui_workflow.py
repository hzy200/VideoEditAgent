from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")


def test_full_gui_workflow_subprocess() -> None:
    """在独立进程运行真实 MainWindow：打开项目→面板→候选→证据→编排→渲染。

    子进程隔离可以避免 Windows/PySide6 在 pytest 进程内 GC 时影响 SQLite
    句柄；探针本身会断言失败并返回非 0。
    """
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("未找到 ffmpeg/ffprobe")
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.setdefault("SMARTCUT_CI", "1")
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "gui_workflow_probe.py")],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = (result.stdout or "") + (result.stderr or "")
    assert result.returncode == 0, output
    assert "GUI probe timings:" in output
    assert "run_status: 1.000" in output, output
    assert "render_output: 1.000" in output, output
    assert "PermissionError" not in output, output
    assert "desktop_task_failed" not in output, output
    assert "shot_clustering_failed" not in output, output
