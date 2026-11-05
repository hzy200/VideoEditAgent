from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from smartcut.desktop.widgets.preview_panel import PreviewPanel


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_duration_updates_label_and_enables_play(qapp) -> None:
    panel = PreviewPanel()
    assert panel.play_button.isEnabled() is False
    panel._on_duration(5000)
    assert panel.play_button.isEnabled() is True
    assert "00:05.000" in panel.time_label.text()


def test_load_media_resets_progress(qapp, monkeypatch) -> None:
    panel = PreviewPanel()
    panel.slider.setRange(0, 10_000)
    panel.slider.setValue(5000)

    class _Player:
        def setSource(self, _url) -> None:
            pass

        def pause(self) -> None:
            pass

    monkeypatch.setattr(panel, "_ensure_player", lambda: True)
    monkeypatch.setattr(panel, "player", _Player())
    panel.load_media("missing.mp4", "源文件预览")
    assert panel.slider.value() == 0
    assert "加载中" in panel.time_label.text()
    assert panel.play_button.isEnabled() is False
