from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from smartcut.desktop.widgets.ai_panel import AIPanel
from smartcut.desktop.widgets.perception_panel import PerceptionPanel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_fast_outline_option_implies_outline(app) -> None:
    panel = AIPanel()
    panel.fast_outline.setChecked(True)
    assert panel.wants_fast_outline() is True
    assert panel.wants_outline() is True


def test_perception_panel_shows_manifest_summary(app, tmp_path) -> None:
    panel = PerceptionPanel()
    panel.set_perception(
        {
            "frame_manifest": [],
            "shots": {"shots": []},
            "quality": {"marks": []},
            "faces": {"anchors": []},
            "chapters": [],
            "video_manifest": {
                "chapters": {"chapters": [{"id": "ch_0001"}]},
                "transcript": {"segment_count": 3},
                "candidates": {"count": 2},
                "visual_notes": [{"chapter_id": "ch_0001"}],
                "cache_keys": {"frames": "f", "chapters": "c"},
            },
        },
        tmp_path,
    )
    text = panel.summary.toPlainText()
    assert "结构化清单" in text
    assert "候选 2" in text
    assert "阶段缓存 2" in text
