from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from smartcut.desktop.widgets.export_dialog import ExportDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_export_dialog_builds_multi_audio_and_subtitle(qapp) -> None:
    dialog = ExportDialog(render_rel=None)
    dialog.container.setCurrentIndex(dialog.container.findData("mkv"))
    dialog.video_codec.setCurrentIndex(dialog.video_codec.findData("libx265"))
    dialog.width.setValue(1280)
    dialog.height.setValue(720)
    dialog.fps.setText("30")
    dialog.audio_streams.setText("0,1")
    dialog.audio_codec.setCurrentIndex(dialog.audio_codec.findData("aac"))
    dialog.subtitle_streams.setText("0")
    dialog.subtitle_codec.setCurrentIndex(dialog.subtitle_codec.findData("ass"))
    dialog.loudness_enabled.setChecked(True)

    request = dialog.request()
    assert request.profile.container == "mkv"
    assert request.profile.video_codec == "libx265"
    assert request.profile.width == 1280
    assert [track.stream_index for track in request.profile.audio_tracks] == [0, 1]
    assert request.profile.audio_tracks[0].codec == "aac"
    assert request.profile.subtitle_tracks[0].codec == "ass"
    assert request.profile.loudness_target == -14.0


def test_export_dialog_copy_mode_forces_passthrough(qapp) -> None:
    dialog = ExportDialog(render_rel=None)
    dialog.video_codec.setCurrentIndex(dialog.video_codec.findData("copy"))
    request = dialog.request()
    assert request.profile.video_codec == "copy"
    assert request.profile.fps_mode == "passthrough"

    dialog.width.setValue(1280)
    dialog.height.setValue(720)
    with pytest.raises(ValueError):
        dialog.request()


def test_export_dialog_audio_only_container(qapp) -> None:
    dialog = ExportDialog(render_rel=None)
    dialog.container.setCurrentIndex(dialog.container.findData("mp3"))
    request = dialog.request()
    assert request.profile.media_type == "audio"
    assert request.profile.container == "mp3"
    assert request.profile.video_codec == "copy"
    assert request.profile.audio_tracks[0].codec == "libmp3lame"


def test_export_dialog_compression_preset(qapp) -> None:
    dialog = ExportDialog(render_rel=None)
    dialog.compression.setCurrentIndex(
        dialog.compression.findData(32)
    )
    assert dialog.crf.value() == 32
    request = dialog.request()
    assert request.profile.crf == 32


def test_export_dialog_audio_asset_source(qapp) -> None:
    dialog = ExportDialog(
        render_rel=None,
        audio_assets=[{"path": "audio/tone.wav", "name": "tone"}],
    )
    dialog.source.setCurrentIndex(
        dialog.source.findData("audio/tone.wav")
    )
    request = dialog.request()
    assert request.src == "audio/tone.wav"


def test_export_dialog_selected_raw_source(qapp) -> None:
    dialog = ExportDialog(
        render_rel=None,
        source="media/raw_broken.mp4",
    )
    assert dialog.source.currentData() == "media/raw_broken.mp4"
    request = dialog.request()
    assert request.src == "media/raw_broken.mp4"


def test_export_dialog_audio_compression_preset(qapp) -> None:
    dialog = ExportDialog(render_rel=None)
    dialog.container.setCurrentIndex(dialog.container.findData("mp3"))
    dialog.compression.setCurrentIndex(
        dialog.compression.findText("小体积")
    )
    assert dialog.audio_bitrate.text() == "96k"
    request = dialog.request()
    assert request.profile.media_type == "audio"
    assert request.profile.audio_tracks[0].bitrate == "96k"
