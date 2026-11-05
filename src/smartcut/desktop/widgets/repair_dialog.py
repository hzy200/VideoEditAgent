"""媒体修复对话框：只构造 RepairRequest，不直接调用 Services。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
)

from smartcut.contracts.media_ops import RepairRequest
from smartcut.contracts.transcode import (
    AUDIO_CONTAINERS,
    AudioOutput,
    TranscodeProfile,
    default_audio_codec,
)

_VIDEO_CONTAINERS = ("mp4", "mov", "mkv")
_STRATEGIES = (
    ("自动诊断", "auto"),
    ("仅重封装", "remux"),
    ("完整重编码", "reencode"),
    ("音画同步", "audio_sync"),
    ("轻度锐化", "sharpen"),
)
_VIDEO_CODECS = {
    "mp4": ("libx264", "libx265", "libsvtav1", "copy"),
    "mov": ("libx264", "libx265", "prores_ks", "copy"),
    "mkv": ("libx264", "libx265", "libsvtav1", "prores_ks", "copy"),
}
_AUDIO_CODECS = {
    "mp4": ("aac", "ac3", "libopus"),
    "mov": ("aac", "ac3", "pcm_s16le", "pcm_s24le"),
    "mkv": ("aac", "ac3", "libopus", "flac", "pcm_s16le"),
    "m4a": ("aac", "ac3", "libopus"),
    "mp3": ("libmp3lame",),
    "wav": ("pcm_s16le", "pcm_s24le"),
    "flac": ("flac",),
}


class RepairDialog(QDialog):
    def __init__(
        self,
        *,
        src: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._src = src
        self.setWindowTitle("媒体修复")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.source_label = QLabel(src or "项目主素材")
        form.addRow("源", self.source_label)

        self.strategy = QComboBox()
        for label, value in _STRATEGIES:
            self.strategy.addItem(label, value)
        form.addRow("修复策略", self.strategy)

        self.container = QComboBox()
        for value in (*_VIDEO_CONTAINERS, *sorted(AUDIO_CONTAINERS)):
            self.container.addItem(value, value)
        self.container.currentIndexChanged.connect(self._refresh_codecs)
        form.addRow("输出容器", self.container)

        self.video_codec = QComboBox()
        form.addRow("视频编码", self.video_codec)
        self.audio_codec = QComboBox()
        form.addRow("音频编码", self.audio_codec)

        self.preset = QComboBox()
        for value in ("ultrafast", "veryfast", "fast", "medium", "slow"):
            self.preset.addItem(value, value)
        self.preset.setCurrentText("medium")
        form.addRow("preset", self.preset)

        self.crf = QSpinBox()
        self.crf.setRange(0, 51)
        self.crf.setValue(20)
        form.addRow("CRF", self.crf)

        self.auto_offset = QCheckBox("自动推断音画偏移")
        self.auto_offset.setChecked(True)
        form.addRow("", self.auto_offset)
        self.offset = QDoubleSpinBox()
        self.offset.setRange(-10.0, 10.0)
        self.offset.setDecimals(3)
        self.offset.setSuffix(" s")
        self.offset.setEnabled(False)
        self.auto_offset.toggled.connect(
            lambda checked: self.offset.setEnabled(not checked)
        )
        form.addRow("音频延迟", self.offset)

        self.sharpen = QDoubleSpinBox()
        self.sharpen.setRange(0.0, 1.0)
        self.sharpen.setDecimals(2)
        self.sharpen.setSingleStep(0.1)
        self.sharpen.setValue(0.5)
        form.addRow("锐化强度", self.sharpen)

        self.dst = QLineEdit()
        self.dst.setPlaceholderText("留空自动写入 repairs/")
        form.addRow("输出路径", self.dst)

        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_codecs()

    def _media_type(self) -> str:
        container = str(self.container.currentData() or "mp4")
        return "audio" if container in AUDIO_CONTAINERS else "video"

    def _refresh_codecs(self) -> None:
        container = str(self.container.currentData() or "mp4")
        audio_only = container in AUDIO_CONTAINERS
        self.video_codec.clear()
        if audio_only:
            self.video_codec.addItem("无视频（仅音频）", "copy")
            self.video_codec.setEnabled(False)
        else:
            self.video_codec.setEnabled(True)
            for value in _VIDEO_CODECS[container]:
                self.video_codec.addItem(value, value)
        self.audio_codec.clear()
        for value in _AUDIO_CODECS[container]:
            self.audio_codec.addItem(value, value)
        if audio_only:
            default = default_audio_codec(container)
            index = self.audio_codec.findData(default)
            if index >= 0:
                self.audio_codec.setCurrentIndex(index)

    def apply_plan(self, plan: dict) -> None:
        """用 AI 解析出的计划预填参数。"""
        operation = str(plan.get("operation") or "")
        repair = plan.get("repair") or {}
        if operation == "repair" and repair.get("strategy"):
            index = self.strategy.findData(repair["strategy"])
            if index >= 0:
                self.strategy.setCurrentIndex(index)
        transcode = plan.get("transcode") or {}
        profile = transcode.get("profile") or {}
        container = profile.get("container")
        if container:
            index = self.container.findData(container)
            if index >= 0:
                self.container.setCurrentIndex(index)
                self._refresh_codecs()
        if profile.get("video_codec"):
            index = self.video_codec.findData(profile["video_codec"])
            if index >= 0:
                self.video_codec.setCurrentIndex(index)
        tracks = profile.get("audio_tracks") or []
        if tracks and tracks[0].get("codec"):
            index = self.audio_codec.findData(tracks[0]["codec"])
            if index >= 0:
                self.audio_codec.setCurrentIndex(index)
        if profile.get("crf") is not None:
            self.crf.setValue(int(profile["crf"]))
        if repair.get("audio_offset_seconds") is not None:
            self.auto_offset.setChecked(False)
            self.offset.setValue(float(repair["audio_offset_seconds"]))
        if repair.get("sharpen_strength") is not None:
            self.sharpen.setValue(float(repair["sharpen_strength"]))

    def accept(self) -> None:  # noqa: D401 - Qt 回调
        try:
            self.request()
        except ValueError as exc:
            QMessageBox.warning(self, "修复参数错误", str(exc))
            return
        super().accept()

    def request(self) -> RepairRequest:
        container = str(self.container.currentData() or "mp4")
        media_type = self._media_type()
        video_codec = (
            "copy"
            if media_type == "audio"
            else str(self.video_codec.currentData() or "libx264")
        )
        audio_codec = str(
            self.audio_codec.currentData() or default_audio_codec(container)
        )
        profile = TranscodeProfile(
            media_type=media_type,
            container=container,
            video_codec=video_codec,
            preset=str(self.preset.currentData() or "medium"),
            crf=self.crf.value(),
            audio_tracks=[AudioOutput(codec=audio_codec)],
        )
        return RepairRequest(
            src=self._src,
            dst=self.dst.text().strip() or None,
            strategy=str(self.strategy.currentData() or "auto"),
            profile=profile,
            audio_offset_seconds=(
                None
                if self.auto_offset.isChecked()
                else float(self.offset.value())
            ),
            sharpen_strength=float(self.sharpen.value()),
        )
