from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from smartcut.desktop.widgets.ai_panel import AIPanel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_audio_first_and_review_controls_exist(app) -> None:
    panel = AIPanel()
    panel.audio_first.setChecked(True)
    assert panel.wants_audio_first() is True
    assert panel.review_button.text() == "审片当前成片"


def test_lock_effects_option(app) -> None:
    panel = AIPanel()
    assert panel.wants_pinned_effects() is False
    panel.lock_effects.setChecked(True)
    assert panel.wants_pinned_effects() is True
