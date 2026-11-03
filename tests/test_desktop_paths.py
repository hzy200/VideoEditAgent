from __future__ import annotations

from pathlib import Path

from smartcut.desktop import paths
from smartcut.desktop.paths import (
    app_data_dir,
    bundled_binary,
    bundled_separator,
    bundled_whisper_dir,
    resource_root,
)


def test_app_data_dir_override(tmp_path: Path) -> None:
    assert app_data_dir(tmp_path) == tmp_path.resolve()


def test_resource_root_is_project_root_in_dev() -> None:
    root = resource_root()
    assert (root / "src" / "smartcut").exists()


def test_bundled_binary_has_fallback() -> None:
    value = bundled_binary("ffmpeg")
    assert isinstance(value, str) and value


def test_bundled_whisper_dir_type() -> None:
    value = bundled_whisper_dir("base")
    assert value is None or isinstance(value, Path)


def test_bundled_separator_discovery(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setattr(paths.sys, "executable", str(tmp_path / "SmartCut.exe"))
    separator = tmp_path / "separator" / "SmartCut-Separator.exe"
    separator.parent.mkdir(parents=True)
    separator.write_bytes(b"x")
    assert bundled_separator() == str(separator)
