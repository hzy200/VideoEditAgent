from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from smartcut.contracts.time import RationalTime
from smartcut.desktop.widgets.outline_panel import OutlinePanel


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _rt(seconds: str) -> dict:
    return RationalTime.from_seconds(seconds, rate=90_000).model_dump()


def test_outline_panel_sends_edited_boundaries(qapp, tmp_path) -> None:
    chapter = {
        "id": "ch_0001",
        "title": "章节",
        "start": _rt("0"),
        "end": _rt("2"),
        "keyframes": [],
    }
    panel = OutlinePanel()
    panel.set_outline(
        {
            "run_id": "run_x",
            "status": "awaiting_approval",
            "approved": False,
            "chapters": [chapter],
        },
        tmp_path,
    )
    panel.table.item(0, 2).setText("0.500")
    panel.table.item(0, 3).setText("1.500")

    captured: list[list[dict]] = []
    panel.approve_requested.connect(lambda items: captured.append(items))
    panel._approve()

    assert captured
    selected = captured[0][0]
    assert RationalTime.model_validate(selected["start"]) == RationalTime.from_seconds(
        "0.5", rate=90_000
    )
    assert RationalTime.model_validate(selected["end"]) == RationalTime.from_seconds(
        "1.5", rate=90_000
    )


def test_outline_panel_keeps_original_when_not_edited(qapp, tmp_path) -> None:
    start = _rt("0.25")
    chapter = {
        "id": "ch_0001",
        "title": "章节",
        "start": start,
        "end": _rt("2"),
        "keyframes": [],
    }
    panel = OutlinePanel()
    panel.set_outline(
        {
            "run_id": "run_x",
            "status": "awaiting_approval",
            "approved": False,
            "chapters": [chapter],
        },
        tmp_path,
    )
    captured: list[list[dict]] = []
    panel.approve_requested.connect(lambda items: captured.append(items))
    panel._approve()
    assert captured[0][0]["start"] == start
