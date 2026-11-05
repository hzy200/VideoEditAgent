from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from smartcut.desktop.widgets.project_panel import ProjectPanel
from smartcut.desktop.widgets.timeline_panel import TimelinePanel
from smartcut.desktop.widgets.timeline_widget import TimelineWidget


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_magnetic_snap_to_playhead(app) -> None:
    widget = TimelineWidget()
    widget.resize(800, 180)
    widget.set_data(
        10.0,
        [{"id": "a", "src": "media/a.mp4", "start": 2.0, "end": 4.0}],
    )
    widget.set_source_durations({"media/a.mp4": 10.0})
    widget.set_position(5.0)
    widget.show()
    app.processEvents()
    lane = widget._lanes()[3]
    y = int(lane.center().y())
    x = int(lane.left() + lane.width() * 3.0 / 10.0)
    target = int(lane.left() + lane.width() * 6.0 / 10.0)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(x, y))
    QTest.mouseMove(widget, QPoint(target, y))
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(target, y))
    app.processEvents()
    assert widget.segments[0]["start"] == pytest.approx(5.0, abs=0.02)
    assert widget.segments[0]["end"] == pytest.approx(7.0, abs=0.02)


def test_output_time_ripple_and_mapping(app) -> None:
    widget = TimelineWidget()
    widget.set_output_time(True)
    widget.set_data(
        10.0,
        [
            {"id": "a", "src": "media/a.mp4", "start": 5.0, "end": 6.0},
            {"id": "b", "src": "media/b.mp4", "start": 0.0, "end": 2.0},
        ],
    )
    widget.set_transitions(
        [
            {
                "after": "a",
                "type": "fade",
                "duration": {"value": 22500, "rate": 90_000},
            }
        ]
    )
    widget.set_data(
        10.0,
        widget.segments,
    )
    assert widget.duration == pytest.approx(2.75, abs=0.01)
    # 第二段源时间 0.5s 映射到输出 1.25s。
    widget.set_position(0.5)
    assert widget.position == pytest.approx(1.25, abs=0.01)
    assert widget._output_to_source(1.25) == pytest.approx(0.5, abs=0.01)
    rect_a = widget._segment_rect(widget.segments[0], widget._lanes()[3])
    rect_b = widget._segment_rect(widget.segments[1], widget._lanes()[3])
    assert rect_a is not None and rect_b is not None
    assert rect_b.left() >= rect_a.left()


def test_multi_track_lanes_and_transform_columns(app) -> None:
    widget = TimelineWidget()
    widget.set_output_time(True)
    widget.set_data(
        10.0,
        [
            {"id": "a", "src": "media/a.mp4", "track": 0, "start": 0, "end": 2},
            {
                "id": "b",
                "src": "media/b.mp4",
                "track": 1,
                "start": 0,
                "end": 2,
                "transform": {
                    "x": 0.6,
                    "y": 0.1,
                    "width": 0.3,
                    "height": 0.3,
                    "opacity": 1.0,
                },
            },
        ],
    )
    lanes = widget._track_lanes()
    assert len(lanes) == 2
    assert lanes[0].top() > lanes[1].top()
    upper = lanes[1]
    x = int(upper.left() + upper.width() * 0.75)
    y = int(upper.center().y())
    assert widget._hit(x, y) == (1, "move")

    panel = TimelinePanel()
    panel.set_ir(
        {
            "revision": 0,
            "media_facts": {
                "src": "media/a.mp4",
                "duration": {"value": 900_000, "rate": 90_000},
                "fps": "30/1",
            },
            "sequence": [
                {
                    "id": "seg_a",
                    "src": "media/a.mp4",
                    "track": 0,
                    "timeline_start": {"value": 0, "rate": 90_000},
                    "range": {
                        "start": {"value": 0, "rate": 90_000},
                        "end": {"value": 90_000, "rate": 90_000},
                    },
                },
                {
                    "id": "seg_b",
                    "src": "media/a.mp4",
                    "track": 1,
                    "timeline_start": {"value": 0, "rate": 90_000},
                    "transform": {
                        "x": 0.6,
                        "y": 0.1,
                        "width": 0.3,
                        "height": 0.3,
                        "opacity": 0.8,
                    },
                    "range": {
                        "start": {"value": 90_000, "rate": 90_000},
                        "end": {"value": 180_000, "rate": 90_000},
                    },
                },
            ],
        }
    )
    assert panel.table.item(1, 6).text() == "1"
    assert panel.table.item(1, 7).text() == "0.600"
    assert panel.segments()[1]["transform"]["opacity"] == pytest.approx(0.8)
    panel.table.selectRow(1)
    panel.set_position(1.5)
    panel.keyframe_button.click()
    assert panel.segments()[1]["keyframes"]

    widget.show()
    app.processEvents()
    changes: list[tuple[int, str, bool]] = []
    widget.track_state_changed.connect(
        lambda track, field, value: changes.append((track, field, value))
    )
    upper = widget._track_lanes()[1]
    QTest.mousePress(
        widget,
        Qt.MouseButton.LeftButton,
        pos=QPoint(int(upper.left() + 30), int(upper.center().y())),
    )
    assert changes and changes[-1] == (1, "muted", True)
    assert widget.track_states[1]["muted"] is True


def test_drag_transition_handle_emits_change(app) -> None:
    widget = TimelineWidget()
    widget.resize(800, 180)
    widget.set_data(
        10.0,
        [
            {"id": "a", "src": "media/a.mp4", "start": 0.0, "end": 5.0},
            {"id": "b", "src": "media/b.mp4", "start": 5.0, "end": 10.0},
        ],
    )
    widget.set_source_durations(
        {"media/a.mp4": 10.0, "media/b.mp4": 10.0}
    )
    widget.set_transitions([])
    widget.default_transition_type = "wipeleft"
    widget.show()
    app.processEvents()
    changes: list[tuple[str, float, str]] = []
    widget.transition_changed.connect(
        lambda after, seconds, kind: changes.append((after, seconds, kind))
    )
    lane = widget._lanes()[3]
    x = int(lane.left() + lane.width() * 5.0 / 10.0)
    y = int(lane.top() + 4)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(x, y))
    QTest.mouseMove(widget, QPoint(x + 50, y))
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(x + 50, y))
    app.processEvents()
    assert changes
    after, seconds, kind = changes[-1]
    assert after == "a"
    assert seconds > 0.1
    assert kind == "wipeleft"


def test_media_bin_and_source_segment(app) -> None:
    panel = ProjectPanel()
    assets = [
        {
            "id": "m1",
            "src": "media/a.mp4",
            "name": "a",
            "kind": "video",
            "is_primary": True,
            "facts": {"duration": {"value": 90_000, "rate": 90_000}},
        },
        {
            "id": "m2",
            "src": "media/b.mp4",
            "name": "b",
            "kind": "video",
            "is_primary": False,
            "facts": {"duration": {"value": 180_000, "rate": 90_000}},
        },
    ]
    panel.set_project_enabled(True)
    panel.set_assets(assets)
    assert panel.media_list.count() == 2
    assert panel.selected_asset()["src"] == "media/a.mp4"
    pip_assets: list[dict] = []
    panel.add_pip.connect(pip_assets.append)
    panel.pip_button.click()
    assert pip_assets and pip_assets[-1]["src"] == "media/a.mp4"

    timeline = TimelinePanel()
    timeline.set_source_assets(assets)
    timeline.set_ir(
        {
            "revision": 0,
            "media_facts": {
                "src": "media/a.mp4",
                "duration": {"value": 900_000, "rate": 90_000},
                "fps": "30/1",
            },
            "sequence": [
                {
                    "id": "seg_a",
                    "src": "media/a.mp4",
                    "range": {
                        "start": {"value": 0, "rate": 90_000},
                        "end": {"value": 90_000, "rate": 90_000},
                    },
                }
            ],
        }
    )
    timeline.add_source_segment(assets[1], 1.0)
    assert timeline.table.rowCount() == 2
    assert timeline.segments()[-1]["src"] == "media/b.mp4"
    assert timeline.table.item(1, 1).text() == "b"
