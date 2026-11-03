from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStyle,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget

    MEDIA_AVAILABLE = True
except Exception:  # pragma: no cover - 取决于打包环境
    QAudioOutput = None  # type: ignore[assignment]
    QMediaPlayer = None  # type: ignore[assignment]
    QVideoWidget = None  # type: ignore[assignment]
    MEDIA_AVAILABLE = False


class PreviewPanel(QWidget):
    position_changed = Signal(float)
    playing_changed = Signal(bool)
    volume_changed = Signal(int)
    mute_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.player = None
        self.audio = None
        self.video = None
        self._updating_slider = False
        self._loop_start: float | None = None
        self._loop_end: float | None = None
        self._looping = False
        self._captions: list[dict] = []
        self._volume = 80
        self._muted = False
        self._caption_style = {
            "font_size": 24,
            "color": "#ffffff",
            "position": "bottom",
        }

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        self.video_frame = QFrame()
        self.video_frame.setObjectName("videoFrame")
        self.video_frame.setMinimumHeight(300)
        self.video_layout = QVBoxLayout(self.video_frame)
        self.video_layout.setContentsMargins(0, 0, 0, 0)
        self.placeholder = QLabel("导入素材后在此预览")
        self.placeholder.setObjectName("videoPlaceholder")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_layout.addWidget(self.placeholder)
        self.subtitle = QLabel(self.video_frame)
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle.setWordWrap(True)
        self.subtitle.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.subtitle.setStyleSheet(
            "color:#ffffff; font-size:24px; background:rgba(0,0,0,140); "
            "padding:4px 10px; border-radius:4px;"
        )
        self.subtitle.hide()
        layout.addWidget(self.video_frame, 1)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.stop_button = QPushButton("停止")
        self.stop_button.setToolTip("停止并回到起点")
        self.play_button = QPushButton("播放")
        self.play_button.setToolTip("播放 / 暂停（空格）")
        self.play_button.setMaximumWidth(96)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.mode_label = QLabel("")
        self.mode_label.setObjectName("mutedText")
        self.loop_label = QLabel("")
        self.loop_label.setObjectName("accentText")
        self.time_label = QLabel("00:00.000 / 00:00.000")
        self.time_label.setObjectName("mutedText")
        self.time_label.setMinimumWidth(170)
        self.mute_button = QPushButton("静音")
        self.mute_button.setToolTip("静音 / 取消静音")
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(self._volume)
        self.volume_slider.setFixedWidth(96)
        self.volume_slider.setToolTip("预览监听音量")
        controls.addWidget(self.stop_button)
        controls.addWidget(self.play_button)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.mode_label)
        controls.addWidget(self.loop_label)
        controls.addWidget(self.time_label)
        controls.addWidget(self.mute_button)
        controls.addWidget(self.volume_slider)
        layout.addLayout(controls)

        self._apply_button_icons()
        self.play_button.clicked.connect(self.toggle_play)
        self.stop_button.clicked.connect(self.stop)
        self.slider.sliderMoved.connect(self._on_slider_moved)
        self.mute_button.clicked.connect(self.toggle_mute)
        self.volume_slider.valueChanged.connect(self.set_volume)

    def _apply_button_icons(self) -> None:
        self.play_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )
        self.stop_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop)
        )
        self._update_volume_button()

    def _update_volume_button(self) -> None:
        icon = (
            QStyle.StandardPixmap.SP_MediaVolumeMuted
            if self._muted
            else QStyle.StandardPixmap.SP_MediaVolume
        )
        self.mute_button.setIcon(self.style().standardIcon(icon))
        self.mute_button.setText("已静音" if self._muted else "静音")
        self.mute_button.setToolTip("取消静音" if self._muted else "静音")

    def _apply_audio_state(self) -> None:
        if self.audio is None:
            return
        self.audio.setVolume(0.0 if self._muted else self._volume / 100.0)

    def set_volume(self, value: int, *, notify: bool = True) -> None:
        self._volume = max(0, min(100, int(value)))
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(self._volume)
        self.volume_slider.blockSignals(False)
        if self._volume > 0 and self._muted:
            self._muted = False
        self._update_volume_button()
        self._apply_audio_state()
        if notify:
            self.volume_changed.emit(self._volume)
            self.mute_changed.emit(self._muted)

    def toggle_mute(self) -> None:
        self._muted = not self._muted
        self._update_volume_button()
        self._apply_audio_state()
        self.mute_changed.emit(self._muted)

    def set_muted(self, muted: bool, *, notify: bool = False) -> None:
        self._muted = bool(muted)
        self._update_volume_button()
        self._apply_audio_state()
        if notify:
            self.mute_changed.emit(self._muted)

    def volume(self) -> int:
        return self._volume

    def is_muted(self) -> bool:
        return self._muted

    def _ensure_player(self) -> bool:
        if self.player is not None:
            return True
        if not MEDIA_AVAILABLE:
            self.placeholder.setText("当前环境不支持 QtMultimedia 预览")
            return False
        try:
            self.video = QVideoWidget(self)
            self.video_layout.addWidget(self.video)
            self.subtitle.raise_()
            self.placeholder.hide()
            self.player = QMediaPlayer(self)
            self.audio = QAudioOutput(self)
            self._apply_audio_state()
            self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video)
            self.player.positionChanged.connect(self._on_position)
            self.player.durationChanged.connect(self._on_duration)
            self.player.playbackStateChanged.connect(self._on_state)
            self.player.errorOccurred.connect(self._on_error)
            return True
        except Exception as exc:  # pragma: no cover
            self.placeholder.setText(f"预览不可用: {exc}")
            self.player = None
            return False

    def load_media(self, path: str | Path, mode: str = "代理预览") -> None:
        if not self._ensure_player():
            return
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.pause()
        self.mode_label.setText(mode)
        self.placeholder.setText("")

    def clear(self) -> None:
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())
        self.slider.setRange(0, 0)
        self.mode_label.setText("")
        self._clear_loop()
        self._captions = []
        self.subtitle.hide()
        self.time_label.setText("00:00.000 / 00:00.000")
        self.placeholder.setText("导入素材后在此预览")
        self.placeholder.show()

    def set_loop(self, start: float, end: float) -> None:
        if end <= start:
            self._clear_loop()
            return
        self._loop_start = max(0.0, float(start))
        self._loop_end = float(end)
        self.loop_label.setText(f"循环 {self._loop_start:.2f}–{self._loop_end:.2f}s")

    def _clear_loop(self) -> None:
        self._loop_start = None
        self._loop_end = None
        self._looping = False
        self.loop_label.setText("")

    def clear_loop(self) -> None:
        self._clear_loop()

    def current_position(self) -> float:
        if self.player is not None:
            return self.player.position() / 1000.0
        return 0.0

    def set_captions(self, captions: list[dict] | None) -> None:
        self._captions = list(captions or [])
        self._update_caption(self.current_position())

    def set_caption_style(
        self,
        *,
        font_size: int = 24,
        color: str = "#ffffff",
        position: str = "bottom",
    ) -> None:
        self._caption_style = {
            "font_size": max(8, min(96, int(font_size))),
            "color": color or "#ffffff",
            "position": position if position in {"top", "middle", "bottom"} else "bottom",
        }
        self.subtitle.setStyleSheet(
            f"color:{self._caption_style['color']}; "
            f"font-size:{self._caption_style['font_size']}px; "
            "background:rgba(0,0,0,140); padding:4px 10px; border-radius:4px;"
        )
        self._layout_subtitle()

    def _update_caption(self, position: float) -> None:
        if not self._captions:
            self.subtitle.hide()
            return
        for cue in self._captions:
            try:
                if float(cue["start"]) <= position < float(cue["end"]):
                    self.subtitle.setText(str(cue.get("text") or ""))
                    self.subtitle.show()
                    self.subtitle.raise_()
                    self._layout_subtitle()
                    return
            except (KeyError, TypeError, ValueError):
                continue
        self.subtitle.hide()

    def _layout_subtitle(self) -> None:
        if self.video_frame.width() <= 0 or self.video_frame.height() <= 0:
            return
        width = max(80, self.video_frame.width() - 40)
        height = 64
        if self._caption_style["position"] == "top":
            y = 20
        elif self._caption_style["position"] == "middle":
            y = max(20, (self.video_frame.height() - height) // 2)
        else:
            y = max(20, self.video_frame.height() - height - 24)
        self.subtitle.setGeometry(20, y, width, height)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_subtitle()

    def toggle_play(self) -> None:
        if self.player is None:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if self._loop_start is not None and self._loop_end is not None:
                position = self.player.position() / 1000.0
                if not (self._loop_start <= position < self._loop_end):
                    self.player.setPosition(int(self._loop_start * 1000))
            self.player.play()

    def stop(self) -> None:
        if self.player is None:
            return
        self.player.stop()
        self.player.setPosition(0)
        self.slider.setValue(0)
        self.time_label.setText(
            f"00:00.000 / {_format_ms(self.player.duration())}"
        )
        self._update_caption(0.0)
        self.position_changed.emit(0.0)

    def seek(self, seconds: float) -> None:
        if self.player is None:
            return
        duration = self.player.duration()
        if duration > 0:
            self.player.setPosition(max(0, min(int(seconds * 1000), duration)))
        self._update_caption(float(seconds))

    def _on_slider_moved(self, value: int) -> None:
        if self.player is not None:
            self.player.setPosition(value)
        self._update_caption(value / 1000.0)
        self.position_changed.emit(value / 1000.0)

    def _on_position(self, position_ms: int) -> None:
        if (
            self._loop_start is not None
            and self._loop_end is not None
            and self.player is not None
            and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            and position_ms >= int(self._loop_end * 1000) - 40
        ):
            if not self._looping:
                self._looping = True
                self.player.setPosition(int(self._loop_start * 1000))
                self._looping = False
            return
        if not self._updating_slider:
            self._updating_slider = True
            self.slider.setValue(position_ms)
            self._updating_slider = False
        self.time_label.setText(
            f"{_format_ms(position_ms)} / {_format_ms(self.player.duration() if self.player else 0)}"
        )
        self._update_caption(position_ms / 1000.0)
        self.position_changed.emit(position_ms / 1000.0)

    def _on_duration(self, duration_ms: int) -> None:
        self.slider.setRange(0, duration_ms)

    def _on_state(self, state) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        icon = (
            QStyle.StandardPixmap.SP_MediaPause
            if playing
            else QStyle.StandardPixmap.SP_MediaPlay
        )
        self.play_button.setIcon(self.style().standardIcon(icon))
        self.play_button.setText("暂停" if playing else "播放")
        self.playing_changed.emit(playing)

    def _on_error(self, _error, message: str) -> None:
        if message:
            self.placeholder.setText(f"播放失败: {message}")


def _format_ms(value: int) -> str:
    if value <= 0:
        return "00:00.000"
    total_seconds = value / 1000.0
    minutes = int(total_seconds // 60)
    seconds = total_seconds - minutes * 60
    return f"{minutes:02d}:{seconds:06.3f}"
