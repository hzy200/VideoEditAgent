"""开发态桌面 smoke test：offscreen 创建主窗口后退出，不访问网络。"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    with tempfile.TemporaryDirectory(prefix="smartcut-smoke-") as directory:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "smartcut.desktop.app",
                "--smoke-test",
                "--data-dir",
                directory,
            ],
            cwd=ROOT,
            env=env,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
