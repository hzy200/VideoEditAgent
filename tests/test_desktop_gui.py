from __future__ import annotations

import os

# offscreen 插件在部分 Windows/PySide6 组合上进程退出码异常；minimal 足够
# 覆盖 widget 逻辑，真正的 offscreen 装配由 test_desktop_smoke 子进程覆盖。
os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from smartcut.config import Settings
from smartcut.desktop.settings_store import DesktopConfig, SettingsStore
from smartcut.desktop.theme import apply_theme
from smartcut.desktop.widgets.audio_panel import AudioPanel
from smartcut.desktop.widgets.candidate_panel import CandidatePanel
from smartcut.desktop.widgets.diagnostics_panel import DiagnosticsPanel
from smartcut.desktop.widgets.inspector_panel import InspectorPanel
from smartcut.desktop.widgets.metrics_panel import MetricsPanel
from smartcut.desktop.widgets.preview_panel import PreviewPanel
from smartcut.desktop.widgets.project_panel import ProjectPanel
from smartcut.desktop.widgets.queue_panel import QueuePanel
from smartcut.desktop.widgets.settings_dialog import SettingsDialog
from smartcut.desktop.widgets.subtitle_panel import SubtitlePanel
from smartcut.desktop.widgets.timeline_panel import TimelinePanel
from smartcut.desktop.widgets.timeline_widget import TimelineWidget


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _cleanup_gui(app):
    yield
    import gc

    app.processEvents()
    gc.collect()
    app.processEvents()


@pytest.fixture(scope="module", autouse=True)
def _teardown_qt(app):
    yield
    import gc

    app.closeAllWindows()
    app.processEvents()
    gc.collect()
    app.processEvents()


def make_ir() -> dict:
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
                "confidence": 0.9,
            },
            {
                "range": {
                    "start": {"value": 450_000, "rate": 90_000},
                    "end": {"value": 630_000, "rate": 90_000},
                },
                "tag": "b",
                "confidence": 0.8,
            },
        ],
    }


def test_timeline_edit_split_delete_move_undo_redo(app) -> None:
    panel = TimelinePanel()
    panel.set_ir(make_ir())
    assert panel.table.rowCount() == 2

    panel.table.selectRow(0)
    panel.set_position(1.5)
    panel.split_selected()
    assert panel.table.rowCount() == 3
    assert panel.segments()[0]["end"] == pytest.approx(1.5)

    panel.undo()
    assert panel.table.rowCount() == 2
    panel.redo()
    assert panel.table.rowCount() == 3

    panel.table.selectRow(1)
    panel.delete_selected()
    assert panel.table.rowCount() == 2

    panel.add_segment(4.0, 5.0, "new")
    assert panel.table.rowCount() == 3
    assert panel.segments()[-1]["tag"] == "new"

    panel.table.selectRow(2)
    panel.move_selected(-1)
    assert panel.segments()[1]["tag"] == "new"

    panel.undo()
    assert panel.segments()[2]["tag"] == "new"


def test_timeline_add_candidates(app) -> None:
    panel = TimelinePanel()
    panel.set_ir(make_ir())
    panel.add_candidates(
        [
            {"start": 2.0, "end": 3.0, "tags": ["主题"]},
            {"start": 6.0, "end": 7.0, "tags": []},
        ]
    )
    assert panel.table.rowCount() == 4
    assert panel.segments()[-2]["tag"] == "主题"
    assert panel.segments()[-1]["tag"] == "候选"


def test_timeline_widget_drag_moves_segment(app) -> None:
    widget = TimelineWidget()
    widget.resize(700, 180)
    widget.set_data(10.0, [{"start": 2.0, "end": 4.0, "tag": "x"}])
    widget.show()
    QApplication.processEvents()
    edited: list[list[dict]] = []
    widget.segments_edited.connect(edited.append)

    _ruler, _thumbs, _wave, lane = widget._lanes()
    x = int(lane.left() + lane.width() * 3.0 / 10.0)
    y = int(lane.center().y())
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(x, y))
    QTest.mouseMove(widget, QPoint(x + int(lane.width() * 0.1), y))
    QTest.mouseRelease(
        widget,
        Qt.MouseButton.LeftButton,
        pos=QPoint(x + int(lane.width() * 0.1), y),
    )
    assert edited
    segment = edited[-1][0]
    assert segment["start"] == pytest.approx(3.0, abs=0.15)
    assert segment["end"] == pytest.approx(5.0, abs=0.15)
    widget.close()
    widget.deleteLater()
    QApplication.processEvents()


def test_preview_loop_state(app) -> None:
    panel = PreviewPanel()
    panel.set_loop(1.0, 2.5)
    assert panel._loop_start == 1.0
    assert "循环" in panel.loop_label.text()
    panel.clear_loop()
    assert panel._loop_start is None


def test_metrics_panel_renders_run_metrics(app) -> None:
    panel = MetricsPanel()
    panel.set_run(
        {
            "metrics": {
                "wall_duration_ms": 1200,
                "totals": {
                    "duration_ms": 800,
                    "queue_wait_ms": 50,
                    "retries": 1,
                    "cache_hit_rate": 0.75,
                    "llm_requests": 2,
                    "total_tokens": 123,
                },
                "encoders": ["h264_qsv"],
                "tasks": [
                    {
                        "agent": "plan",
                        "task_id": "plan",
                        "status": "succeeded",
                        "queue_wait_ms": 10,
                        "duration_ms": 500,
                        "retries": 0,
                        "cache_hits": 1,
                        "cache_misses": 0,
                        "total_tokens": 123,
                        "encoder": None,
                    }
                ],
            }
        }
    )
    assert panel.table.rowCount() == 1
    assert "缓存命中率" in panel.summary.text()
    assert panel.table.item(0, 0).text() == "plan"


def test_candidate_panel_adds_selected(app) -> None:
    panel = CandidatePanel()
    panel.set_candidates(
        {
            "candidates": [
                {
                    "id": "cand_0001",
                    "source_range": {
                        "start": {"value": 180_000, "rate": 90_000},
                        "end": {"value": 270_000, "rate": 90_000},
                    },
                    "score": 0.8,
                    "text": "topic",
                    "tags": ["主题"],
                }
            ],
            "source": "fts",
        }
    )
    added: list[list[dict]] = []
    panel.add_requested.connect(added.append)
    panel.table.selectRow(0)
    panel._emit_add()
    assert added and added[0][0]["start"] == pytest.approx(2.0)
    assert added[0][0]["end"] == pytest.approx(3.0)


def test_candidate_panel_loads_keyframe_thumbnail(app, tmp_path) -> None:
    image = QImage(32, 18, QImage.Format.Format_RGB32)
    image.fill(QColor("#336699"))
    frame = tmp_path / "frame.jpg"
    assert image.save(str(frame))

    panel = CandidatePanel()
    panel.set_workspace_root(tmp_path)
    panel.set_candidates(
        {
            "candidates": [
                {
                    "id": "cand_0001",
                    "source_range": {
                        "start": {"value": 0, "rate": 90_000},
                        "end": {"value": 90_000, "rate": 90_000},
                    },
                    "visual_hash": 1,
                    "keyframe": "frame.jpg",
                }
            ],
            "source": "visual",
        }
    )
    for _ in range(50):
        app.processEvents()
        QTest.qWait(20)
        item = panel.table.item(0, 0)
        if item is not None and not item.icon().isNull():
            break

    item = panel.table.item(0, 0)
    assert item is not None
    assert not item.icon().isNull()


def test_settings_dialog_exposes_long_video_and_caption_fields(app, tmp_path) -> None:
    store = SettingsStore(tmp_path)
    config = DesktopConfig(
        perception_max_frames=600,
        transcribe_workers=2,
        transcribe_silence_rms=0.003,
        transcribe_silence_min_seconds=0.7,
        audio_filler_words="um,uh",
        caption_font_size=30,
        caption_color="#FFCC00",
        caption_position="top",
        export_preset="mp4_h264",
    )
    settings = Settings(workspace_root=tmp_path, deepseek_api_key=None)
    dialog = SettingsDialog(config, settings, store, tmp_path)
    result, _api, _clear, _vision, _clear_vision = dialog.values()
    assert result.perception_max_frames == 600
    assert result.transcribe_workers == 2
    assert result.transcribe_silence_rms == pytest.approx(0.003)
    assert result.transcribe_silence_min_seconds == pytest.approx(0.7)
    assert result.audio_filler_words == "um,uh"
    assert result.caption_font_size == 30
    assert result.caption_color == "#FFCC00"
    assert result.caption_position == "top"
    assert result.export_preset == "mp4_h264"


def test_inspector_diff_emit_and_show(app) -> None:
    panel = InspectorPanel()
    panel.set_revisions([{"revision": 1}, {"revision": 2}])
    panel.select_revision(2)
    requested: list[tuple[int, int]] = []
    panel.diff_requested.connect(lambda a, b: requested.append((a, b)))
    panel._emit_diff()
    assert requested == [(2, 1)]

    panel.show_diff(
        {
            "base_revision": 1,
            "revision": 2,
            "added_count": 1,
            "removed_count": 1,
            "unchanged_count": 0,
            "added": [{"start_seconds": "1", "end_seconds": "2", "tag": "a"}],
            "removed": [{"start_seconds": "3", "end_seconds": "4", "tag": "b"}],
            "unchanged": [],
        }
    )
    text = panel.diff_browser.toPlainText()
    assert "+ 1–2s a" in text
    assert "- 3–4s b" in text


def test_timeline_zoom_pan_waveform_and_thumbnails(app) -> None:
    widget = TimelineWidget()
    widget.resize(900, 180)
    widget.set_data(100.0, [{"start": 10.0, "end": 20.0, "tag": "x"}])
    widget.zoom_by(2.0, 25.0)
    assert widget.zoom == pytest.approx(2.0)
    assert widget.visible_duration == pytest.approx(50.0)
    start = widget.view_start
    widget.pan_by(10.0)
    assert widget.view_start == pytest.approx(start + 10.0)
    widget.set_waveform([0.0, 0.5, 1.0])
    assert widget.waveform == [0.0, 0.5, 1.0]
    pixmap = QPixmap(20, 12)
    pixmap.fill(Qt.GlobalColor.blue)
    widget.set_thumbnails([(12.0, pixmap)])
    assert widget.thumbnails
    widget.fit()
    assert widget.zoom == 1.0
    assert widget.view_start == 0.0


def test_preview_caption_overlay_and_style(app) -> None:
    panel = PreviewPanel()
    panel.set_caption_style(font_size=30, color="#ffcc00", position="top")
    panel.set_captions(
        [{"start": 1.0, "end": 2.0, "text": "hello caption"}]
    )
    panel._update_caption(1.5)
    assert panel.subtitle.text() == "hello caption"
    assert "#ffcc00" in panel.subtitle.styleSheet()
    panel._update_caption(3.0)
    assert panel.subtitle.isHidden()


def test_timeline_frame_manifest_async(app, tmp_path) -> None:
    image = QImage(32, 18, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    image_path = tmp_path / "frame.jpg"
    assert image.save(str(image_path))
    panel = TimelinePanel()
    panel.set_ir(make_ir())
    panel.set_frame_manifest(
        [{"t": {"value": 0, "rate": 90_000}, "path": image_path.name}],
        tmp_path,
    )
    panel._thumb_cache._pool.waitForDone(2000)
    QApplication.processEvents()
    assert panel.timeline.thumbnails


def test_timeline_diff_highlight(app) -> None:
    panel = TimelinePanel()
    panel.set_ir(make_ir())
    panel.set_diff(
        {
            "added_count": 1,
            "removed_count": 1,
            "unchanged_count": 1,
            "added": [{"start_seconds": "1", "end_seconds": "3"}],
            "removed": [{"start_seconds": "5", "end_seconds": "7"}],
            "unchanged": [{"start_seconds": "1", "end_seconds": "3"}],
        }
    )
    assert "diff" in panel.status_label.text()
    assert (1.0, 3.0) in panel.timeline._diff_added
    assert (5.0, 7.0) in panel.timeline._diff_removed


def test_settings_dialog_theme_accessibility_fields(app, tmp_path) -> None:
    store = SettingsStore(tmp_path)
    config = DesktopConfig(
        theme="light",
        ui_scale=1.25,
        high_contrast=True,
        language="zh",
    )
    settings = Settings(workspace_root=tmp_path, deepseek_api_key=None)
    dialog = SettingsDialog(config, settings, store, tmp_path)
    result, *_ = dialog.values()
    assert result.theme == "light"
    assert result.ui_scale == pytest.approx(1.25)
    assert result.high_contrast is True
    assert result.language == "zh"


def test_settings_store_roundtrips_window_state(tmp_path) -> None:
    store = SettingsStore(tmp_path)
    config = DesktopConfig(
        theme="light",
        ui_scale=1.1,
        high_contrast=True,
        window_geometry="Z2VvbQ==",
        window_state="c3RhdGU=",
        splitter_sizes=[300, 700, 400],
        center_splitter_sizes=[500, 300],
        tab_index=3,
        last_project_id="p_abc",
    )
    store.save(config)
    loaded = store.load()
    assert loaded.theme == "light"
    assert loaded.ui_scale == pytest.approx(1.1)
    assert loaded.high_contrast is True
    assert loaded.window_geometry == "Z2VvbQ=="
    assert loaded.window_state == "c3RhdGU="
    assert loaded.splitter_sizes == [300, 700, 400]
    assert loaded.center_splitter_sizes == [500, 300]
    assert loaded.tab_index == 3
    assert loaded.last_project_id == "p_abc"


def test_project_panel_rename_delete_replace_signals(app) -> None:
    panel = ProjectPanel()
    panel.set_projects(
        [
            {"id": "p_1", "name": "one", "workspace_dir": "/tmp/one"},
            {"id": "p_2", "name": "two", "workspace_dir": "/tmp/two"},
        ]
    )
    panel.select_project("p_2")
    assert panel.selected_project_id() == "p_2"
    panel.set_project_enabled(True)

    renamed: list[str] = []
    deleted: list[str] = []
    imported: list[int] = []
    panel.rename_project.connect(renamed.append)
    panel.delete_project.connect(deleted.append)
    panel.import_media.connect(lambda: imported.append(1))

    panel.rename_button.click()
    panel.delete_button.click()
    panel.replace_button.click()
    assert renamed == ["p_2"]
    assert deleted == ["p_2"]
    assert imported == [1]


def test_queue_panel_lists_and_cancels_selected(app) -> None:
    panel = QueuePanel()
    panel.set_jobs(
        {
            "project_id": "p_1",
            "jobs": [
                {
                    "id": "job_a",
                    "kind": "render",
                    "status": "running",
                    "progress": 40,
                    "message": "渲染中",
                    "created_at": "2026-01-01T00:00:00",
                },
                {
                    "id": "job_b",
                    "kind": "transcribe",
                    "status": "queued",
                    "progress": 0,
                    "message": "排队中",
                    "created_at": "2026-01-01T00:00:01",
                },
            ],
        }
    )
    assert panel.table.rowCount() == 2
    assert "2 个任务" in panel.summary.text()
    assert "2 个排队/运行中" in panel.summary.text()
    assert panel.cancel_button.isEnabled()

    requested: list[list[str]] = []
    panel.cancel_requested.connect(requested.append)
    panel.table.selectRow(0)
    panel.cancel_button.click()
    assert requested == [["job_a"]]


def test_diagnostics_panel_renders_info_and_logs(app) -> None:
    panel = DiagnosticsPanel()
    panel.set_diagnostics(
        {
            "info": {"workspace_root": "D:/ws", "active_jobs": 1},
            "logs": ["log-a\n", "log-b\n"],
        }
    )
    text = panel.info.toPlainText()
    assert "workspace_root: D:/ws" in text
    assert "active_jobs: 1" in text
    assert panel.logs.toPlainText() == "log-a\nlog-b\n"


def test_apply_theme_scales_font_and_contrast(app) -> None:
    original_font = app.font()
    original_style = app.styleSheet()
    original_palette = app.palette()
    try:
        apply_theme(app, "dark", scale=1.5, contrast=True)
        assert "facc15" in app.styleSheet()
        assert app.font().pointSizeF() >= original_font.pointSizeF()
        apply_theme(app, "light", scale=1.0, contrast=False)
        assert "facc15" not in app.styleSheet()
    finally:
        app.setFont(original_font)
        app.setStyleSheet(original_style)
        app.setPalette(original_palette)


def test_unsaved_prompt_logic(app, monkeypatch) -> None:
    from smartcut.desktop.main_window import MainWindow

    monkeypatch.setattr(
        "smartcut.desktop.main_window._headless_ui",
        lambda: False,
    )
    ctx = type("Ctx", (), {"unsaved": True})()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel),
    )
    assert MainWindow._resolve_unsaved(ctx, "退出") == "cancel"
    assert ctx.unsaved is True

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Discard),
    )
    assert MainWindow._resolve_unsaved(ctx, "退出") == "discard"
    assert ctx.unsaved is False

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Save),
    )
    ctx.unsaved = True
    assert MainWindow._resolve_unsaved(ctx, "退出") == "save"

    ctx.unsaved = False
    assert MainWindow._resolve_unsaved(ctx, "退出") == "discard"


def test_capture_window_state_merges_layout_and_project(app) -> None:
    from PySide6.QtCore import QByteArray

    from smartcut.desktop.main_window import MainWindow

    class _Sizes:
        def __init__(self, values):
            self.values = values

        def sizes(self):
            return list(self.values)

    class _Tabs:
        def currentIndex(self):
            return 4

    class _Geometry:
        @staticmethod
        def toBase64():
            return QByteArray(b"geometry")

    class _Preview:
        @staticmethod
        def volume():
            return 42

        @staticmethod
        def is_muted():
            return True

    ctx = type(
        "Ctx",
        (),
        {
            "config": DesktopConfig(),
            "main_splitter": _Sizes([300, 700, 400]),
            "center_splitter": _Sizes([500, 300]),
            "tabs": _Tabs(),
            "preview_panel": _Preview(),
            "controller": type("C", (), {"current_project": "p_1"})(),
            "saveGeometry": staticmethod(lambda: _Geometry()),
            "saveState": staticmethod(lambda: _Geometry()),
        },
    )()
    MainWindow._capture_window_state(ctx)
    assert ctx.config.splitter_sizes == [300, 700, 400]
    assert ctx.config.center_splitter_sizes == [500, 300]
    assert ctx.config.tab_index == 4
    assert ctx.config.last_project_id == "p_1"
    assert ctx.config.window_geometry == "geometry"
    assert ctx.config.window_state == "geometry"
    assert ctx.config.preview_volume == 42
    assert ctx.config.preview_muted is True


def test_preview_volume_mute_and_stop_state(app) -> None:
    panel = PreviewPanel()
    volumes: list[int] = []
    muted: list[bool] = []
    panel.volume_changed.connect(volumes.append)
    panel.mute_changed.connect(muted.append)

    panel.set_volume(35)
    assert panel.volume() == 35
    assert volumes[-1] == 35
    assert panel.is_muted() is False

    panel.toggle_mute()
    assert panel.is_muted() is True
    assert muted[-1] is True
    assert panel.mute_button.text() == "已静音"

    # 调整音量会自动解除静音，符合常见播放器行为。
    panel.set_volume(20)
    assert panel.is_muted() is False
    assert volumes[-1] == 20

    panel.stop()  # 无媒体时也应安全
    assert panel.volume_slider.value() == 20


def test_subtitle_panel_add_edit_delete_and_validation(app) -> None:
    panel = SubtitlePanel()
    emitted: list[list[dict]] = []
    panel.cues_changed.connect(emitted.append)

    panel.set_cues([{"start": 1.0, "end": 2.0, "text": "hello"}], burn=True)
    assert panel.table.rowCount() == 1
    assert panel.burn() is True
    assert emitted == []

    panel.add_cue(3.0, 5.0, "world")
    assert panel.table.rowCount() == 2
    assert emitted[-1][-1]["text"] == "world"

    panel.table.item(0, 2).setText("hello edited")
    assert emitted[-1][0]["text"] == "hello edited"

    before = len(emitted)
    panel.table.item(0, 0).setText("not-a-number")
    assert len(emitted) == before
    assert "不是数字" in panel.status_label.text()
    panel.table.item(0, 0).setText("1.000")

    panel.table.selectRow(1)
    panel.remove_selected()
    assert panel.table.rowCount() == 1
    assert emitted[-1][0]["text"] == "hello edited"

    panel.clear_cues()
    assert emitted[-1] == []


def test_audio_panel_values_and_reset(app) -> None:
    panel = AudioPanel()
    emitted: list[dict] = []
    panel.audio_changed.connect(emitted.append)

    panel.set_audio({"volume_db": -3.5, "pitch_semitones": 4})
    assert panel.volume_slider.value() == -35
    assert panel.pitch_slider.value() == 4
    assert "-3.5 dB" in panel.volume_label.text()
    assert "+4 半音" in panel.pitch_label.text()

    panel.volume_slider.setValue(-45)
    assert emitted[-1] == {"volume_db": -4.5, "pitch_semitones": 4}

    panel.pitch_slider.setValue(-2)
    assert emitted[-1] == {"volume_db": -4.5, "pitch_semitones": -2}

    panel.reset()
    assert emitted[-1] == {"volume_db": 0.0, "pitch_semitones": 0.0}
    assert panel.volume_slider.value() == 0
    assert panel.pitch_slider.value() == 0


def test_settings_dialog_demucs_fields(app, tmp_path) -> None:
    store = SettingsStore(tmp_path)
    config = DesktopConfig(
        demucs_command="demucs --two-stems=vocals",
        demucs_model="htdemucs_ft",
    )
    settings = Settings(workspace_root=tmp_path, deepseek_api_key=None)
    dialog = SettingsDialog(config, settings, store, tmp_path)
    result, *_ = dialog.values()
    assert result.demucs_command == "demucs --two-stems=vocals"
    assert result.demucs_model == "htdemucs_ft"


def test_timeline_audio_lane_tracks_and_waveforms(app) -> None:
    widget = TimelineWidget()
    widget.resize(700, 220)
    widget.set_data(10.0, [{"start": 1.0, "end": 3.0, "tag": "x"}])
    widget.set_audio_tracks(
        [
            {
                "id": "main",
                "name": "原声",
                "asset_id": "main",
                "role": "main",
                "timeline_start": {"value": 0, "rate": 90_000},
                "source_start": {"value": 0, "rate": 90_000},
                "source_end": {"value": 900_000, "rate": 90_000},
            },
            {
                "id": "bgm",
                "name": "BGM",
                "asset_id": "a_bgm",
                "role": "music",
                "timeline_start": {"value": 180_000, "rate": 90_000},
                "source_start": {"value": 0, "rate": 90_000},
                "source_end": {"value": 450_000, "rate": 90_000},
                "loop": True,
                "solo": True,
            },
        ]
    )
    widget.set_audio_waveforms(
        {"main": [0.1, 0.5, 0.2], "assets": {"a_bgm": [0.4, 0.8]}}
    )
    assert len(widget._lanes_full()) == 5
    assert widget.audio_tracks[1]["timeline_start"] == pytest.approx(2.0)
    assert widget.audio_tracks[1]["loop"] is True
    assert widget.audio_waveforms["a_bgm"] == [0.4, 0.8]
    assert not widget.grab().isNull()


def test_audio_panel_accepts_ir_rational_tracks(app) -> None:
    panel = AudioPanel()
    panel.set_assets(
        [{"id": "a_bgm", "name": "BGM", "facts": {"duration": {"value": 5, "rate": 1}}}]
    )
    panel.set_tracks(
        [
            {
                "id": "main",
                "role": "main",
                "asset_id": "main",
                "source_start": {"value": 0, "rate": 90_000},
                "source_end": {"value": 900_000, "rate": 90_000},
                "timeline_start": {"value": 0, "rate": 90_000},
                "gain_db": 0,
                "fade_in": {"value": 0, "rate": 90_000},
                "fade_out": {"value": 0, "rate": 90_000},
            },
            {
                "id": "t_01",
                "role": "music",
                "asset_id": "a_bgm",
                "source_start": {"value": 0, "rate": 90_000},
                "source_end": {"value": 450_000, "rate": 90_000},
                "timeline_start": {"value": 90_000, "rate": 90_000},
                "gain_db": -6,
                "fade_in": {"value": 18_000, "rate": 90_000},
                "fade_out": {"value": 0, "rate": 90_000},
            },
        ],
        main_duration=10.0,
    )
    assert panel.table.rowCount() == 2
    assert panel.table.item(1, 2).text() == "1.000"
    assert panel.table.item(1, 7).text() == "0.20"
    tracks = panel.tracks()
    assert tracks[1]["source_end"] == pytest.approx(5.0)
    assert tracks[1]["timeline_start"] == pytest.approx(1.0)
    assert tracks[1]["gain_db"] == pytest.approx(-6.0)
    assert tracks[1]["loop"] is False


def test_audio_panel_track_editing(app) -> None:
    panel = AudioPanel()
    panel.set_assets(
        [
            {
                "id": "a_bgm",
                "name": "BGM",
                "facts": {"duration": {"value": 5, "rate": 1}},
            }
        ]
    )
    panel.set_tracks(None, main_duration=10.0)
    assert panel.table.rowCount() == 1
    assert panel.tracks()[0]["asset_id"] == "main"

    emitted: list[list[dict]] = []
    panel.tracks_changed.connect(emitted.append)
    panel.add_selected_asset()
    assert panel.table.rowCount() == 2
    assert len(emitted[-1]) == 2
    assert emitted[-1][-1]["asset_id"] == "a_bgm"
    assert emitted[-1][-1]["role"] == "music"

    panel.table.item(1, 3).setText("-6.5")
    assert emitted[-1][1]["gain_db"] == pytest.approx(-6.5)

    panel.table.item(1, 4).setCheckState(Qt.CheckState.Checked)
    assert emitted[-1][1]["muted"] is True

    panel.table.item(1, 5).setCheckState(Qt.CheckState.Checked)
    assert emitted[-1][1]["solo"] is True

    panel.table.item(1, 6).setCheckState(Qt.CheckState.Checked)
    assert emitted[-1][1]["loop"] is True

    before = len(emitted)
    panel.table.item(1, 2).setText("bad")
    assert len(emitted) == before
    assert "无效数字" in panel.status_label.text()
    panel.table.item(1, 2).setText("1.5")

    panel.table.selectRow(1)
    panel.remove_selected()
    assert panel.table.rowCount() == 1
    assert len(emitted[-1]) == 1

    panel.set_tracks([], main_duration=10.0)
    assert panel.table.rowCount() == 0
    assert "静音" in panel.status_label.text()
