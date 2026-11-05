from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from smartcut.desktop.widgets.keyframe_dialog import KeyframeDialog
from smartcut.desktop.widgets.preview_panel import PipBoxOverlay


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_pip_overlay_drag_moves_box(app) -> None:
    overlay = PipBoxOverlay()
    overlay.resize(400, 300)
    overlay.set_pips(
        [
            {
                "id": "pip_1",
                "x": 0.6,
                "y": 0.1,
                "width": 0.3,
                "height": 0.3,
                "opacity": 1.0,
            }
        ]
    )
    overlay.show()
    app.processEvents()
    changes: list[tuple[str, dict]] = []
    overlay.pip_changed.connect(
        lambda pip_id, value: changes.append((pip_id, value))
    )
    start = QPoint(int(0.75 * 400), int(0.25 * 300))
    QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(overlay, QPoint(start.x() + 40, start.y() + 30))
    QTest.mouseRelease(
        overlay,
        Qt.MouseButton.LeftButton,
        pos=QPoint(start.x() + 40, start.y() + 30),
    )
    app.processEvents()
    assert changes
    pip_id, transform = changes[-1]
    assert pip_id == "pip_1"
    assert transform["x"] == pytest.approx(0.7, abs=0.02)
    assert transform["y"] == pytest.approx(0.2, abs=0.02)
    assert transform["width"] == pytest.approx(0.3, abs=0.02)


def test_keyframe_dialog_edit_and_delete(app) -> None:
    dialog = KeyframeDialog(
        [
            {
                "t": {"value": 0, "rate": 90_000},
                "x": 0.6,
                "y": 0.1,
                "width": 0.3,
                "height": 0.3,
                "opacity": 1.0,
            }
        ]
    )
    assert dialog.table.rowCount() == 1
    dialog.table.item(0, 1).setText("0.2")
    dialog.table.item(0, 0).setText("0.500")
    dialog._add_row()
    assert dialog.table.rowCount() == 2
    dialog.table.selectRow(1)
    dialog._delete_row()
    assert dialog.table.rowCount() == 1
    keyframes = dialog.keyframes()
    assert len(keyframes) == 1
    assert keyframes[0]["x"] == pytest.approx(0.2)
    assert keyframes[0]["t"]["value"] == 45_000
