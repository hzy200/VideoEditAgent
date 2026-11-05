from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from smartcut.desktop.settings_store import DesktopConfig, SettingsStore
from smartcut.desktop.theme import apply_theme
from smartcut.desktop.widgets.audio_panel import AudioPanel
from smartcut.desktop.widgets.diagnostics_panel import DiagnosticsPanel
from smartcut.desktop.widgets.inspector_panel import InspectorPanel
from smartcut.desktop.widgets.preview_panel import PreviewPanel
from smartcut.desktop.widgets.project_panel import ProjectPanel
from smartcut.desktop.widgets.queue_panel import QueuePanel
from smartcut.desktop.widgets.subtitle_panel import SubtitlePanel
from smartcut.desktop.widgets.timeline_panel import TimelinePanel
from smartcut.desktop.widgets.timeline_widget import TimelineWidget


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _cleanup(app):
    yield
    app.processEvents()


def _ir() -> dict:
    return {
        "revision": 1,
        "media_facts": {
            "duration": {"value": 900_000, "rate": 90_000},
            "fps": "30/1",
        },
        "sequence": [
            {
                "range": {
                    "start": {"value": 90_000, "rate": 90_000},
                    "end": {"value": 270_000, "rate": 90_000},
                },
                "tag": "a",
            },
            {
                "range": {
                    "start": {"value": 450_000, "rate": 90_000},
                    "end": {"value": 630_000, "rate": 90_000},
                },
                "tag": "b",
            },
        ],
    }


def test_theme_dark_light_palette(app) -> None:
    original_font = app.font()
    original_style = app.styleSheet()
    original_palette = app.palette()
    try:
        apply_theme(app, "dark", scale=1.0)
        assert app.palette().color(QPalette.ColorRole.Window).name() == "#0a0e15"
        assert "#38bdf8" in app.styleSheet()
        apply_theme(app, "light", scale=1.0)
        assert app.palette().color(QPalette.ColorRole.Window).name() == "#eef2f8"
        assert app.palette().color(QPalette.ColorRole.Base).name() == "#fbfdff"
    finally:
        app.setFont(original_font)
        app.setStyleSheet(original_style)
        app.setPalette(original_palette)


def test_settings_store_corrupt_and_unknown(tmp_path) -> None:
    store = SettingsStore(tmp_path)
    store.path.write_text("{not-json", encoding="utf-8")
    assert store.load().theme == "dark"
    store.path.write_text(
        json.dumps({"theme": "light", "unknown": 1}), encoding="utf-8"
    )
    loaded = store.load()
    assert loaded.theme == "light"
    assert not hasattr(loaded, "unknown")


def test_preview_caption_positions(app) -> None:
    panel = PreviewPanel()
    panel.video_frame.resize(800, 400)
    panel.set_caption_style(position="top", font_size=24, color="#ffffff")
    panel.set_captions([{"start": 0, "end": 2, "text": "x"}])
    panel._update_caption(1.0)
    panel._layout_subtitle()
    assert panel.subtitle.y() == 20

    panel.set_caption_style(position="middle")
    panel._layout_subtitle()
    assert panel.subtitle.y() == (400 - 64) // 2

    panel.set_caption_style(position="bottom")
    panel._layout_subtitle()
    assert panel.subtitle.y() == 400 - 64 - 24


def test_timeline_zoom_pan_clamps(app) -> None:
    widget = TimelineWidget()
    widget.set_data(10.0, [])
    widget.zoom_by(1000.0, 5.0)
    assert widget.zoom == pytest.approx(50.0)
    widget.pan_by(-1000)
    assert widget.view_start == 0.0
    widget.pan_by(1000)
    assert widget.view_start <= 10.0 - widget.visible_duration + 1e-6


def test_timeline_panel_undo_redo_clear(app) -> None:
    panel = TimelinePanel()
    panel.set_ir(_ir())
    panel.table.selectRow(0)
    panel.set_position(2.0)
    panel.split_selected()
    assert panel.table.rowCount() == 3
    panel.undo()
    assert panel.table.rowCount() == 2
    panel.redo()
    assert panel.table.rowCount() == 3
    panel.clear()
    assert panel.table.rowCount() == 0
    assert "尚无版本" in panel.status_label.text()


def test_audio_panel_stem_role_inference(app) -> None:
    panel = AudioPanel()
    panel.set_assets(
        [
            {"id": "a_v", "name": "人声", "facts": {"duration": {"value": 2, "rate": 1}}},
            {"id": "a_i", "name": "伴奏", "facts": {"duration": {"value": 2, "rate": 1}}},
        ]
    )
    panel.set_tracks([], main_duration=10.0)
    panel.asset_combo.setCurrentIndex(0)
    panel.add_selected_asset()
    panel.asset_combo.setCurrentIndex(1)
    panel.add_selected_asset()
    tracks = panel.tracks()
    assert [track["role"] for track in tracks] == ["stem_vocals", "stem_instrumental"]


def test_audio_panel_remove_all_is_explicit_silence(app) -> None:
    panel = AudioPanel()
    emitted: list[list[dict]] = []
    panel.tracks_changed.connect(emitted.append)
    panel.set_tracks(None, main_duration=10.0)
    panel.table.selectRow(0)
    panel.remove_selected()
    assert emitted[-1] == []
    assert "静音" in panel.status_label.text()


def test_subtitle_panel_signals(app) -> None:
    panel = SubtitlePanel()
    burns: list[bool] = []
    reloads: list[bool] = []
    panel.burn_changed.connect(burns.append)
    panel.reload_requested.connect(lambda: reloads.append(True))
    panel.burn_checkbox.setChecked(True)
    panel.reload_button.click()
    assert burns[-1] is True
    assert reloads == [True]


def test_queue_and_diagnostics_empty_states(app) -> None:
    queue = QueuePanel()
    queue.set_jobs({"jobs": []})
    assert "0 个任务" in queue.summary.text()
    assert not queue.cancel_button.isEnabled()

    diagnostics = DiagnosticsPanel()
    diagnostics.set_diagnostics({"info": {}, "logs": []})
    assert "暂无诊断信息" in diagnostics.info.toPlainText()
    assert diagnostics.logs.toPlainText() == ""


def test_project_and_inspector_empty_states(app) -> None:
    project = ProjectPanel()
    project.set_project_enabled(True)
    project.set_projects([])
    assert not project.import_button.isEnabled()
    assert not project.rename_button.isEnabled()

    inspector = InspectorPanel()
    inspector.set_revisions([])
    assert not inspector.save_button.isEnabled()
    assert not inspector.render_button.isEnabled()
