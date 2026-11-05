from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from smartcut.desktop.widgets.media_tools_panel import MediaToolsPanel


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_panel_buttons_only_emit_signals(qapp) -> None:
    panel = MediaToolsPanel()
    events: list[str] = []
    panel.import_requested.connect(lambda: events.append("import"))
    panel.diagnose_requested.connect(lambda: events.append("diagnose"))
    panel.repair_requested.connect(lambda: events.append("repair"))
    panel.convert_requested.connect(lambda: events.append("convert"))

    panel.set_project_enabled(True)
    panel.import_button.click()
    panel.set_project_enabled(True)
    panel.diagnose_button.click()
    panel.set_project_enabled(True)
    panel.repair_button.click()
    panel.set_project_enabled(True)
    panel.convert_button.click()
    assert events == ["import", "diagnose", "repair", "convert"]


def test_panel_source_selection_and_results(qapp) -> None:
    panel = MediaToolsPanel()
    panel.set_render_rel("renders/r1/output.mp4")
    panel.set_audio_assets(
        [{"path": "audio/tone.wav", "name": "tone"}]
    )
    panel.add_source("media/raw_broken.mp4", "raw_broken.mp4")
    assert panel.source.count() == 4
    assert panel.selected_source() == "media/raw_broken.mp4"

    panel.source.setCurrentIndex(panel.source.findData("__render__"))
    assert panel.selected_source() == "renders/r1/output.mp4"
    panel.source.setCurrentIndex(panel.source.findData("audio/tone.wav"))
    assert panel.selected_source() == "audio/tone.wav"
    panel.source.setCurrentIndex(panel.source.findData("media/raw_broken.mp4"))
    assert panel.selected_source() == "media/raw_broken.mp4"

    panel.show_diagnosis(
        {
            "src": "media/a.mp4",
            "probe_ok": True,
            "recommended": "audio_sync",
            "issues": [
                {"severity": "warning", "message": "音画起始时间不一致"}
            ],
            "warnings": ["结构性修复"],
        }
    )
    text = panel.result.toPlainText()
    assert "audio_sync" in panel.status.text()
    assert "音画起始时间不一致" in text
    assert "结构性修复" in text

    panel.show_result(
        {
            "operation": "repair",
            "output_path": "repairs/a.mp4",
            "used_strategy": "reencode",
            "cached": False,
        }
    )
    assert "repairs/a.mp4" in panel.status.text()
    assert "reencode" in panel.result.toPlainText()
