"""桌面导出对话框：只构造 TranscodeRequest，不直接调用 Services。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
)

from smartcut.contracts.export_presets import EXPORT_PRESETS, preset_target
from smartcut.contracts.transcode import (
    AudioOutput,
    MetadataOptions,
    SubtitleOutput,
    TranscodeProfile,
    TranscodeRequest,
)

_CONTAINER_VIDEO = {
    "mp4": ["copy", "libx264", "libx265", "libsvtav1"],
    "mov": ["copy", "libx264", "libx265", "prores_ks"],
    "mkv": ["copy", "libx264", "libx265", "libsvtav1", "prores_ks"],
}
_CONTAINER_AUDIO = {
    "mp4": ["copy", "aac", "ac3", "libopus"],
    "mov": ["copy", "aac", "ac3", "pcm_s16le", "pcm_s24le"],
    "mkv": ["copy", "aac", "ac3", "libopus", "flac", "pcm_s16le", "pcm_s24le"],
}
_CONTAINER_SUBTITLE = {
    "mp4": ["mov_text"],
    "mov": ["mov_text"],
    "mkv": ["mov_text", "srt", "ass", "webvtt"],
}


class ExportDialog(QDialog):
    def __init__(
        self,
        *,
        render_rel: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._render_rel = render_rel
        self.setWindowTitle("导出 / 转码")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.source = QComboBox()
        self.source.addItem("项目素材", None)
        if render_rel:
            self.source.addItem("当前成片", "render")
        form.addRow("源", self.source)

        self.platform = QComboBox()
        self.platform.addItem("自定义", "custom")
        for name in ("reels", "douyin", "youtube"):
            preset = EXPORT_PRESETS[name]
            self.platform.addItem(preset.description, name)
        self.platform.currentIndexChanged.connect(self._apply_platform)
        form.addRow("平台预设", self.platform)

        self.container = QComboBox()
        for value in ("mp4", "mov", "mkv"):
            self.container.addItem(value, value)
        self.container.currentIndexChanged.connect(self._refresh_codecs)
        form.addRow("容器", self.container)

        self.video_codec = QComboBox()
        self.video_codec.currentIndexChanged.connect(self._on_video_codec_changed)
        form.addRow("视频编码", self.video_codec)

        self.preset = QComboBox()
        for value in ("ultrafast", "veryfast", "fast", "medium", "slow"):
            self.preset.addItem(value, value)
        self.preset.setCurrentText("medium")
        form.addRow("preset", self.preset)

        self.crf = QSpinBox()
        self.crf.setRange(0, 63)
        self.crf.setValue(20)
        form.addRow("CRF", self.crf)

        self.video_bitrate = QLineEdit()
        self.video_bitrate.setPlaceholderText("如 6M；留空用 CRF")
        form.addRow("视频码率", self.video_bitrate)

        self.width = QSpinBox()
        self.width.setRange(0, 16384)
        self.width.setSpecialValueText("源宽度")
        self.height = QSpinBox()
        self.height.setRange(0, 16384)
        self.height.setSpecialValueText("源高度")
        form.addRow("输出宽度", self.width)
        form.addRow("输出高度", self.height)

        self.scale_mode = QComboBox()
        for value in ("fit", "crop", "stretch"):
            self.scale_mode.addItem(value, value)
        form.addRow("缩放方式", self.scale_mode)

        self.fps = QLineEdit()
        self.fps.setPlaceholderText("如 30 或 30000/1001；留空保持")
        form.addRow("帧率", self.fps)

        self.fps_mode = QComboBox()
        for value in ("cfr", "vfr", "passthrough"):
            self.fps_mode.addItem(value, value)
        form.addRow("帧率模式", self.fps_mode)

        self.pix_fmt = QLineEdit()
        self.pix_fmt.setPlaceholderText("留空按编码器默认，如 yuv420p10le")
        form.addRow("像素格式", self.pix_fmt)

        self.keep_10bit = QCheckBox("保留 10bit（HEVC/AV1/ProRes）")
        form.addRow("", self.keep_10bit)
        self.hdr_tonemap = QCheckBox("HDR → SDR tonemap")
        form.addRow("", self.hdr_tonemap)
        self.autorotate = QCheckBox("自动旋转")
        self.autorotate.setChecked(True)
        form.addRow("", self.autorotate)
        self.faststart = QCheckBox("faststart")
        self.faststart.setChecked(True)
        form.addRow("", self.faststart)

        self.audio_codec = QComboBox()
        form.addRow("音频编码", self.audio_codec)
        self.audio_bitrate = QLineEdit("192k")
        form.addRow("音频码率", self.audio_bitrate)
        self.audio_channels = QSpinBox()
        self.audio_channels.setRange(0, 32)
        self.audio_channels.setSpecialValueText("源声道")
        form.addRow("音频声道", self.audio_channels)
        self.audio_streams = QLineEdit("0")
        self.audio_streams.setPlaceholderText("源音频流索引，逗号分隔；留空不输出音频")
        form.addRow("音频流", self.audio_streams)

        self.subtitle_codec = QComboBox()
        form.addRow("字幕编码", self.subtitle_codec)
        self.subtitle_streams = QLineEdit()
        self.subtitle_streams.setPlaceholderText("源字幕流索引，逗号分隔；留空不输出字幕")
        form.addRow("字幕流", self.subtitle_streams)

        self.loudness = QDoubleSpinBox()
        self.loudness.setRange(-70.0, 0.0)
        self.loudness.setValue(-14.0)
        self.loudness.setDecimals(1)
        self.loudness_enabled = QCheckBox("响度归一 LUFS")
        form.addRow("", self.loudness_enabled)
        form.addRow("目标 LUFS", self.loudness)
        self.two_pass = QCheckBox("两遍 loudnorm")
        self.two_pass.setChecked(True)
        form.addRow("", self.two_pass)

        self.metadata_title = QLineEdit()
        self.metadata_artist = QLineEdit()
        form.addRow("标题", self.metadata_title)
        form.addRow("作者", self.metadata_artist)

        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_codecs()

    def _refresh_codecs(self) -> None:
        container = str(self.container.currentData() or "mp4")
        self.video_codec.clear()
        for value in _CONTAINER_VIDEO[container]:
            self.video_codec.addItem(value, value)
        self.audio_codec.clear()
        for value in _CONTAINER_AUDIO[container]:
            self.audio_codec.addItem(value, value)
        self.subtitle_codec.clear()
        for value in _CONTAINER_SUBTITLE[container]:
            self.subtitle_codec.addItem(value, value)
        self._on_video_codec_changed()

    def _apply_platform(self) -> None:
        name = str(self.platform.currentData() or "custom")
        target = preset_target(name)
        if target is None:
            return
        self.container.setCurrentIndex(
            max(0, self.container.findData("mp4"))
        )
        self._refresh_codecs()
        self.video_codec.setCurrentIndex(
            max(0, self.video_codec.findData("libx264"))
        )
        width, height = target
        self.width.setValue(width)
        self.height.setValue(height)
        self.scale_mode.setCurrentIndex(
            max(0, self.scale_mode.findData("fit" if name == "youtube" else "crop"))
        )
        self.fps.setText("30")
        self.fps_mode.setCurrentIndex(max(0, self.fps_mode.findData("cfr")))
        self.crf.setValue(18 if name == "youtube" else 20)

    def _on_video_codec_changed(self) -> None:
        copy_mode = str(self.video_codec.currentData() or "") == "copy"
        for widget in (
            self.width,
            self.height,
            self.fps,
            self.keep_10bit,
            self.hdr_tonemap,
        ):
            widget.setEnabled(not copy_mode)
        if copy_mode:
            self.fps_mode.setCurrentIndex(
                max(0, self.fps_mode.findData("passthrough"))
            )
            self.fps_mode.setEnabled(False)
        else:
            self.fps_mode.setEnabled(True)

    @staticmethod
    def _indices(text: str) -> list[int]:
        values: list[int] = []
        for item in text.split(","):
            item = item.strip()
            if not item:
                continue
            values.append(int(item))
        return values

    def accept(self) -> None:  # noqa: D401 - Qt 回调
        try:
            self.request()
        except ValueError as exc:
            QMessageBox.warning(self, "导出参数错误", str(exc))
            return
        super().accept()

    def request(self) -> TranscodeRequest:
        container = str(self.container.currentData() or "mp4")
        source = self.source.currentData()
        src = self._render_rel if source == "render" else None
        if source == "render" and not self._render_rel:
            raise ValueError("当前成片不存在，无法导出成片")

        width = self.width.value() or None
        height = self.height.value() or None
        fps = self.fps.text().strip() or None
        pix_fmt = self.pix_fmt.text().strip() or None
        video_bitrate = self.video_bitrate.text().strip() or None

        audio_tracks = [
            AudioOutput(
                stream_index=index,
                codec=str(self.audio_codec.currentData() or "aac"),
                bitrate=self.audio_bitrate.text().strip() or None,
                channels=self.audio_channels.value() or None,
            )
            for index in self._indices(self.audio_streams.text())
        ]
        subtitle_tracks = [
            SubtitleOutput(
                stream_index=index,
                codec=str(self.subtitle_codec.currentData() or "mov_text"),
            )
            for index in self._indices(self.subtitle_streams.text())
        ]
        metadata = MetadataOptions(
            title=self.metadata_title.text().strip() or None,
            artist=self.metadata_artist.text().strip() or None,
        )
        profile = TranscodeProfile(
            container=container,
            video_codec=str(self.video_codec.currentData() or "libx264"),
            preset=str(self.preset.currentData() or "medium"),
            crf=self.crf.value(),
            video_bitrate=video_bitrate,
            width=width,
            height=height,
            scale_mode=str(self.scale_mode.currentData() or "fit"),
            fps=fps,
            fps_mode=str(self.fps_mode.currentData() or "cfr"),
            pix_fmt=pix_fmt,
            keep_10bit=self.keep_10bit.isChecked(),
            hdr_tonemap=self.hdr_tonemap.isChecked(),
            autorotate=self.autorotate.isChecked(),
            faststart=self.faststart.isChecked(),
            loudness_target=(
                self.loudness.value() if self.loudness_enabled.isChecked() else None
            ),
            loudnorm_two_pass=self.two_pass.isChecked(),
            audio_tracks=audio_tracks,
            subtitle_tracks=subtitle_tracks,
            metadata=metadata,
        )
        return TranscodeRequest(src=src, profile=profile)
