from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from smartcut.config import Settings
from smartcut.runner.execute import ToolCallExecutor
from smartcut.runner.paths import Workspace


def _require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        # CI 缺少 ffmpeg 时不能让几十个媒体测试静默跳过；必须直接失败，
        # 否则“全绿”只代表测试没跑。
        if os.environ.get("SMARTCUT_CI") == "1":
            pytest.fail(f"CI 环境缺少 {name}：媒体测试不得静默跳过")
        pytest.skip(f"未找到 {name}，跳过需要 ffmpeg 的测试")
    return path


@pytest.fixture(scope="session")
def ffmpeg_bin() -> str:
    return _require_binary("ffmpeg")


@pytest.fixture(scope="session")
def ffprobe_bin() -> str:
    return _require_binary("ffprobe")


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspace"
    for name in ("media", "audio", "proxy", "frames", "renders", "tmp", "logs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return Workspace(root)


@pytest.fixture
def settings(workspace: Workspace, ffmpeg_bin: str, ffprobe_bin: str) -> Settings:
    return Settings(
        workspace_root=workspace.root,
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
        max_workers=2,
        deepseek_api_key=None,
        hardware_encoding="off",
        hardware_decoding="off",
        # 旧视觉测试保持 contact sheet/keyframe 路径；M0.6 证据引用有专项测试。
        vision_use_evidence=False,
    )


@pytest.fixture
def executor(settings: Settings, workspace: Workspace) -> ToolCallExecutor:
    return ToolCallExecutor(settings, workspace)


@pytest.fixture
def synthetic_video(tmp_path: Path, ffmpeg_bin: str) -> Path:
    """2 秒、320x240@30fps、含 AAC 音频的合成素材。"""
    path = tmp_path / "source.mp4"
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=30:duration=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "30",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        str(path),
    ]
    subprocess.run(command, check=True, capture_output=True)
    return path


@pytest.fixture
def ingested_source(workspace: Workspace, synthetic_video: Path) -> str:
    destination = workspace.media_dir / "source.mp4"
    shutil.copy2(synthetic_video, destination)
    return destination.relative_to(workspace.root).as_posix()


def pytest_sessionfinish(session, exitstatus) -> None:
    """Windows/PySide6 偶发在解释器退出阶段返回 127；测试全绿时强制干净退出。"""
    if exitstatus != 0:
        return
    try:
        import PySide6  # noqa: F401
    except Exception:
        return
    import atexit
    import os

    atexit.register(lambda: os._exit(0))
