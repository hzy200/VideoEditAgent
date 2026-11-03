from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from smartcut.desktop.widgets.timing_panel import TimingPanel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _rt_dict(value: int) -> dict:
    return {"value": value, "rate": 90_000}


def test_timing_panel_roundtrip_and_validation(app) -> None:
    panel = TimingPanel()
    panel.set_data(
        [{"id": "seg_a", "tag": "intro"}, {"id": "seg_b", "tag": "body"}],
        {"seg_a": 1.5},
        [
            {
                "after": "seg_a",
                "type": "fade",
                "duration": _rt_dict(45_000),
            }
        ],
        [
            {
                "text": "水印",
                "position": "top_left",
                "font_size": 20,
                "color": "#FFFFFF",
                "opacity": 0.5,
                "start": _rt_dict(0),
            }
        ],
    )
    payload = panel.payload()
    assert payload["speeds"]["seg_a"] == pytest.approx(1.5)
    assert payload["transitions"][0]["after"] == "seg_a"
    assert payload["overlays"][0]["text"] == "水印"
    assert panel.errors() == []

    panel._speed_spins[0][1].setValue(2.0)
    assert panel.payload()["speeds"]["seg_a"] == pytest.approx(2.0)

    panel._transition_rows[0]["duration"].setValue(0.0)
    assert panel.payload()["transitions"] == []

    panel.add_overlay()
    assert len(panel.payload()["overlays"]) == 2

    panel.overlay_table.cellWidget(0, 6).setText("bad")
    assert panel.errors()


def test_image_overlay_and_removal_payload(app) -> None:
    panel = TimingPanel()
    panel.set_overlay_assets([{"id": "o_logo", "name": "logo"}])
    panel.add_overlay()
    kind = panel.overlay_table.cellWidget(0, 0)
    kind.setCurrentIndex(kind.findData("image"))
    asset = panel.overlay_table.cellWidget(0, 2)
    asset.setCurrentIndex(asset.findData("o_logo"))
    panel.overlay_table.cellWidget(0, 5).setValue(0.3)
    overlay = panel.payload()["overlays"][0]
    assert overlay["kind"] == "image"
    assert overlay["asset_id"] == "o_logo"
    assert overlay["scale"] == pytest.approx(0.3)
    assert "text" not in overlay

    panel.add_removal()
    removal = panel.payload()["removals"][0]
    assert removal["x"] >= 1
    assert removal["width"] > 0
    assert panel.errors() == []
