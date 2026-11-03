from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from smartcut.contracts.effects import ColorEffect
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.time import RationalTime
from smartcut.desktop.controller import DesktopController
from smartcut.desktop.edit_session import EditSession, EditSnapshot
from smartcut.desktop.main_window import MainWindow
from smartcut.desktop.settings_store import SettingsStore
from smartcut.desktop.widgets.effects_panel import EffectsPanel
from smartcut.desktop.widgets.timeline_panel import TimelinePanel
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _pump(app, controller: DesktopController, rounds: int = 30) -> None:
    for _ in range(rounds):
        controller.pool.waitForDone(500)
        app.processEvents()


def test_effects_panel_add_edit_preset_and_remove(app) -> None:
    panel = EffectsPanel()
    emitted: list[list[dict]] = []
    panel.effects_changed.connect(emitted.append)

    panel.type_combo.setCurrentIndex(panel.type_combo.findData("color"))
    panel.add_current_type()
    assert len(panel.effects()) == 1
    assert panel.effects()[0]["type"] == "color"
    assert emitted[-1][0]["type"] == "color"

    panel._set_param("brightness", 0.25)
    assert panel.effects()[0]["brightness"] == pytest.approx(0.25)

    panel.preset_combo.setCurrentText("黑白")
    panel.apply_preset()
    assert panel.effects()[0]["saturation"] == pytest.approx(0.0)

    panel.table.selectRow(0)
    panel.remove_selected()
    assert panel.effects() == []


def test_effects_panel_enabled_toggle_and_invalid_skip(app) -> None:
    panel = EffectsPanel()
    panel.set_effects([{"type": "blur", "radius": 3.0}])
    assert panel.effects()[0]["enabled"] is True

    item = panel.table.item(0, 0)
    item.setCheckState(Qt.CheckState.Unchecked)
    assert panel.effects()[0]["enabled"] is False

    panel.set_effects([{"type": "speed"}])
    assert panel.effects() == []


def test_effects_panel_segment_scope_and_badge(app) -> None:
    panel = EffectsPanel()
    panel.set_segments([{"id": "seg_a", "tag": "intro"}])
    panel.add_scope_combo.setCurrentIndex(
        panel.add_scope_combo.findData("seg_a")
    )
    panel.type_combo.setCurrentIndex(panel.type_combo.findData("blur"))
    panel.add_current_type()
    effect = panel.effects()[0]
    assert effect["target"] == "segment"
    assert effect["segment_id"] == "seg_a"
    assert panel.table.item(0, 1).text() == "片段 1 · intro"

    timeline = TimelinePanel()
    timeline.set_effect_segments(["seg_a"])
    assert timeline.timeline.effect_segments == {"seg_a"}


def test_timeline_panel_assigns_stable_segment_ids(app) -> None:
    ir = {
        "media_facts": {
            "src": "media/a.mp4",
            "duration": {"value": 900_000, "rate": 90_000},
        },
        "sequence": [
            {
                "range": {
                    "start": {"value": 0, "rate": 90_000},
                    "end": {"value": 180_000, "rate": 90_000},
                },
                "tag": "a",
            }
        ],
    }
    panel = TimelinePanel()
    panel.set_ir(ir)
    first = panel.segments()[0]["id"]
    assert first
    panel.set_ir(ir)
    assert panel.segments()[0]["id"] == first


def test_edit_session_undo_redo_and_dirty() -> None:
    session = EditSession()
    saved = EditSnapshot(sequence=[{"start": 0.0, "end": 1.0}], effects=[])
    session.reset(saved)
    assert not session.dirty

    changed = EditSnapshot(
        sequence=[{"start": 0.0, "end": 2.0}],
        effects=[{"type": "blur", "radius": 2.0}],
    )
    assert session.capture(changed) is True
    assert session.dirty
    assert session.undo() == saved
    assert not session.dirty
    assert session.redo() == changed
    assert session.dirty
    session.mark_saved()
    assert not session.dirty


def test_render_effects_preview_downscales_and_caches(
    tmp_path: Path, settings, synthetic_video: Path
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project = services.create_project("preview")
        ingested = services.ingest(project["id"], str(synthetic_video))
        facts = MediaFacts.model_validate(ingested["facts"])
        ir = EditIR(
            project_id=project["id"],
            revision=0,
            media_facts=facts,
            sequence=[
                TrimOp(
                    src=ingested["src"],
                    range=TimeRange(start=_rt("0"), end=_rt("2")),
                )
            ],
            effects=[ColorEffect(brightness=0.2)],
        )

        first = services.render_effects_preview(
            project["id"], ir, max_height=120
        )
        assert first["legacy"] is False
        assert "error" not in first, first.get("error")
        path = Path(first["path"])
        assert path.exists()

        workspace = services.project_workspace(project["id"])
        relative = path.relative_to(workspace.root).as_posix()
        rendered = services.probe(project["id"], relative)
        assert rendered.height == 120
        assert rendered.width == 160

        second = services.render_effects_preview(
            project["id"], ir, max_height=120
        )
        assert second["cached"] is True
        assert second["path"] == first["path"]
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_main_window_manual_effect_roundtrip(
    app, tmp_path: Path, settings, synthetic_video: Path, monkeypatch
) -> None:
    modal_calls: list[tuple] = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        staticmethod(lambda *args, **kwargs: modal_calls.append(args)),
    )
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    controller = DesktopController(settings, services, jobs)
    store = SettingsStore(tmp_path / "data")
    window = None
    try:
        project = services.create_project("effects")
        ingested = services.ingest(project["id"], str(synthetic_video))
        facts = MediaFacts.model_validate(ingested["facts"])
        services.submit_revision(
            project["id"],
            EditIR(
                project_id=project["id"],
                revision=0,
                media_facts=facts,
                sequence=[
                    TrimOp(
                        src=ingested["src"],
                        range=TimeRange(start=_rt("0.2"), end=_rt("1.2")),
                    )
                ],
            ),
        )
        pytest.importorskip("PIL")
        from PIL import Image

        logo = tmp_path / "logo.png"
        Image.new("RGBA", (80, 40), (220, 30, 30, 255)).save(logo)
        overlay_asset = services.import_overlay_asset(
            project["id"], str(logo), name="logo"
        )

        window = MainWindow(controller, settings, store, tmp_path / "data")
        _pump(app, controller, rounds=10)
        controller.open_project(project["id"])
        _pump(app, controller)
        assert window.current_ir is not None

        # 后台错误必须非阻塞：离屏环境下不得调用模态 QMessageBox.critical。
        window._on_error("模拟后台错误")
        assert modal_calls == []
        assert "模拟后台错误" in window.status_label.text()
        assert "模拟后台错误" in window.diagnostics_panel.logs.toPlainText()

        modal_info: list[tuple] = []
        modal_questions: list[tuple] = []
        modal_execs: list[tuple] = []
        monkeypatch.setattr(
            QMessageBox,
            "information",
            staticmethod(lambda *args, **kwargs: modal_info.append(args)),
        )
        monkeypatch.setattr(
            QMessageBox,
            "question",
            staticmethod(lambda *args, **kwargs: modal_questions.append(args)),
        )
        monkeypatch.setattr(
            QDialog,
            "exec",
            lambda self, *args, **kwargs: modal_execs.append((self, args)),
        )
        window._on_resumable_runs([{"id": "run_x", "goal": "恢复"}])
        window._on_render_reviewed(
            {"summary": "审片完成", "score": 0.9, "issues": []}
        )
        window._show_json("计划", {"steps": []})
        assert modal_info == []
        assert modal_questions == []
        assert modal_execs == []

        window.unsaved = True
        assert window._resolve_unsaved("关闭") == "discard"
        assert modal_questions == []

        panel = window.effects_panel
        panel.type_combo.setCurrentIndex(panel.type_combo.findData("color"))
        panel.add_current_type()
        assert window.unsaved is True
        assert len(panel.effects()) == 1

        window._undo()
        assert panel.effects() == []
        window._redo()
        assert len(panel.effects()) == 1

        window.timing_panel._speed_spins[0][1].setValue(1.5)
        window.timing_panel.add_overlay()
        overlay_kind = window.timing_panel.overlay_table.cellWidget(0, 0)
        overlay_kind.setCurrentIndex(overlay_kind.findData("image"))
        asset_combo = window.timing_panel.overlay_table.cellWidget(0, 2)
        asset_combo.setCurrentIndex(
            asset_combo.findData(overlay_asset["id"])
        )
        window.timing_panel.overlay_table.cellWidget(0, 5).setValue(0.25)
        window.timing_panel.add_removal()

        window._on_save_revision()
        _pump(app, controller)

        revisions = services.list_revisions(project["id"])
        latest_no = max(int(item["revision"]) for item in revisions)
        latest = services.get_revision(project["id"], latest_no)
        assert latest.effects
        assert latest.effects[0].type == "color"
        assert latest.sequence[0].speed == pytest.approx(1.5)
        assert latest.overlays
        assert latest.overlays[0].kind == "image"
        assert latest.overlays[0].asset_id == overlay_asset["id"]
        assert latest.removals
        assert window.unsaved is False
        assert modal_calls == []
    finally:
        if window is not None:
            window.unsaved = False
            window.close()
            window.deleteLater()
            app.processEvents()
        controller.shutdown()
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
