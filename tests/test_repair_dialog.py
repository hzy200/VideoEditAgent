from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from smartcut.desktop.widgets.repair_dialog import RepairDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_repair_dialog_defaults_to_auto(qapp) -> None:
    dialog = RepairDialog(src="media/a.mp4")
    request = dialog.request()
    assert request.src == "media/a.mp4"
    assert request.strategy == "auto"
    assert request.profile.media_type == "video"
    assert request.profile.video_codec == "libx264"
    assert request.profile.audio_tracks[0].codec == "aac"


def test_repair_dialog_audio_only_container(qapp) -> None:
    dialog = RepairDialog()
    index = dialog.container.findData("mp3")
    assert index >= 0
    dialog.container.setCurrentIndex(index)
    request = dialog.request()
    assert request.profile.media_type == "audio"
    assert request.profile.container == "mp3"
    assert request.profile.video_codec == "copy"
    assert request.profile.audio_tracks[0].codec == "libmp3lame"


def test_repair_dialog_sync_offset_and_sharpen(qapp) -> None:
    dialog = RepairDialog()
    dialog.strategy.setCurrentIndex(dialog.strategy.findData("audio_sync"))
    dialog.auto_offset.setChecked(False)
    dialog.offset.setValue(0.5)
    dialog.sharpen.setValue(0.7)
    request = dialog.request()
    assert request.strategy == "audio_sync"
    assert request.audio_offset_seconds == pytest.approx(0.5)
    assert request.sharpen_strength == pytest.approx(0.7)


def test_repair_dialog_applies_plan(qapp) -> None:
    dialog = RepairDialog()
    dialog.apply_plan(
        {
            "operation": "repair",
            "repair": {
                "strategy": "sharpen",
                "audio_offset_seconds": -0.2,
                "sharpen_strength": 0.8,
            },
            "transcode": {"profile": {"container": "mkv"}},
        }
    )
    request = dialog.request()
    assert request.strategy == "sharpen"
    assert request.audio_offset_seconds == pytest.approx(-0.2)
    assert request.sharpen_strength == pytest.approx(0.8)
    assert request.profile.container == "mkv"
