from __future__ import annotations

import importlib.util
import os
import shutil

import pytest

import conftest as test_conftest


def test_require_binary_fails_in_ci_when_missing(monkeypatch) -> None:
    monkeypatch.setenv("SMARTCUT_CI", "1")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(pytest.fail.Exception):
        test_conftest._require_binary("ffmpeg")


def test_require_binary_skips_outside_ci_when_missing(monkeypatch) -> None:
    monkeypatch.delenv("SMARTCUT_CI", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(pytest.skip.Exception):
        test_conftest._require_binary("ffmpeg")


def test_ci_environment_has_ffmpeg_binaries() -> None:
    if os.environ.get("SMARTCUT_CI") != "1":
        pytest.skip("仅在 CI 环境校验 ffmpeg/ffprobe 真实存在")
    for name in ("ffmpeg", "ffprobe"):
        assert shutil.which(name), f"CI 环境缺少 {name}，媒体测试会静默减少"


def test_ci_environment_has_optional_test_dependencies() -> None:
    if os.environ.get("SMARTCUT_CI") != "1":
        pytest.skip("仅在 CI 环境校验可选测试依赖已安装")
    # 这些模块被测试在模块顶层导入；缺失会在收集阶段直接报错。
    required = {
        "PIL": "vision",
        "cv2": "vision",
        "numpy": "vision",
        "scenedetect": "vision",
        "fastapi": "server/dev",
        "faster_whisper": "transcribe",
    }
    missing = [
        f"{module}({extra})"
        for module, extra in required.items()
        if importlib.util.find_spec(module) is None
    ]
    assert not missing, f"CI 安装 extras 不完整，缺少: {missing}"
