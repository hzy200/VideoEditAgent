from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSlider,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from smartcut.desktop.theme import apply_standard_icon

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget

    MEDIA_AVAILABLE = True
except Exception:  # pragma: no cover - 取决于打包环境
    QAudioOutput = None  # type: ignore[assignment]
    QMediaPlayer = None  # type: ignore[assignment]
    QVideoWidget = None  # type: ignore[assignment]
    MEDIA_AVAILABLE = False


class PipBoxOverlay(QWidget):
    """预览区画中画框选：拖动移动，右下角手柄缩放。"""

    pip_changed = Signal(str, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pips: list[dict] = []
        self.selected = -1
        self._drag: dict | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setStyleSheet("background:transparent;")
        self.setMouseTracking(True)

    def set_pips(self, pips: list[dict] | None) -> None:
        self.pips = [dict(item) for item in pips or []]
        if self.selected >= len(self.pips):
            self.selected = -1
        self.update()

    def _rect(self, pip: dict) -> QRectF:
        return QRectF(
            float(pip.get("x", 0.0)) * self.width(),
            float(pip.get("y", 0.0)) * self.height(),
            max(12.0, float(pip.get("width", 1.0)) * self.width()),
            max(12.0, float(pip.get("height", 1.0)) * self.height()),
        )

    def paintEvent(self, _event) -> None:
        if not self.pips:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for index, pip in enumerate(self.pips):
            rect = self._rect(pip)
            selected = index == self.selected
            painter.setPen(
                QPen(QColor("#22d3ee"), 2 if selected else 1, Qt.PenStyle.DashLine)
            )
            painter.setBrush(QColor(34, 211, 238, 24))
            painter.drawRect(rect)
            if selected:
                painter.setBrush(QColor("#22d3ee"))
                painter.drawRect(
                    QRectF(rect.right() - 6, rect.bottom() - 6, 10, 10)
                )

    def mousePressEvent(self, event) -> None:
        point = event.position()
        for index in range(len(self.pips) - 1, -1, -1):
            rect = self._rect(self.pips[index])
            handle = QRectF(rect.right() - 8, rect.bottom() - 8, 12, 12)
            if handle.contains(point):
                self.selected = index
                self._drag = {"index": index, "mode": "resize", "point": point}
                self.update()
                return
            if rect.contains(point):
                self.selected = index
                self._drag = {"index": index, "mode": "move", "point": point}
                self.update()
                return
        self.selected = -1
        self._drag = None
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._drag is None or not self.pips:
            return
        index = int(self._drag["index"])
        if index >= len(self.pips):
            return
        pip = dict(self.pips[index])
        dx = (event.position().x() - self._drag["point"].x()) / max(1, self.width())
        dy = (event.position().y() - self._drag["point"].y()) / max(1, self.height())
        if self._drag["mode"] == "move":
            pip["x"] = min(max(0.0, float(pip.get("x", 0.0)) + dx), 1.0 - float(pip.get("width", 0.1)))
            pip["y"] = min(max(0.0, float(pip.get("y", 0.0)) + dy), 1.0 - float(pip.get("height", 0.1)))
        else:
            pip["width"] = max(0.05, min(1.0 - float(pip.get("x", 0.0)), float(pip.get("width", 0.1)) + dx))
            pip["height"] = max(0.05, min(1.0 - float(pip.get("y", 0.0)), float(pip.get("height", 0.1)) + dy))
        self.pips[index] = pip
        self._drag["point"] = event.position()
        self.update()

    def mouseReleaseEvent(self, _event) -> None:
        if self._drag is None:
            return
        index = int(self._drag["index"])
        self._drag = None
        if 0 <= index < len(self.pips):
            pip = self.pips[index]
            self.pip_changed.emit(
                str(pip.get("id") or ""),
                {
                    "x": float(pip.get("x", 0.0)),
                    "y": float(pip.get("y", 0.0)),
                    "width": float(pip.get("width", 1.0)),
                    "height": float(pip.get("height", 1.0)),
                    "opacity": float(pip.get("opacity", 1.0)),
                },
            )


class PreviewPanel(QWidget):
    position_changed = Signal(float)
    playing_changed = Signal(bool)
    volume_changed = Signal(int)
    mute_changed = Signal(bool)
    step_requested = Signal(int)
    pip_changed = Signal(str, object)

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
        # 记录预览模式切换，便于诊断“源文件→代理”是否发生。
        self.mode_history: list[str] = []
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
        self.pip_overlay = PipBoxOverlay(self.video_frame)
        self.pip_overlay.pip_changed.connect(self.pip_changed.emit)
        self.pip_overlay.hide()
        layout.addWidget(self.video_frame, 1)

        controls = QHBoxLayout()
        controls.setSpacing(5)

        def transport_button(tooltip: str, size: QSize) -> QToolButton:
            button = QToolButton()
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonIconOnly
            )
            button.setIconSize(QSize(16, 16))
            button.setFixedSize(size)
            button.setToolTip(tooltip)
            return button

        self.prev_button = transport_button("上一帧（←）", QSize(30, 28))
        self.play_button = transport_button(
            "播放 / 暂停（空格）", QSize(34, 30)
        )
        self.play_button.setObjectName("transportPlay")
        self.next_button = transport_button("下一帧（→）", QSize(30, 28))
        self.stop_button = transport_button("停止并回到起点", QSize(30, 28))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.mode_label = QLabel("")
        self.mode_label.setObjectName("mutedText")
        self.loop_label = QLabel("")
        self.loop_label.setObjectName("accentText")
        self.time_label = QLabel("00:00.000 / 00:00.000")
        self.time_label.setObjectName("mutedText")
        self.time_label.setMinimumWidth(170)
        self.mute_button = transport_button("静音 / 取消静音", QSize(30, 28))
        self.mute_button.setText("静音")
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(self._volume)
        self.volume_slider.setFixedWidth(88)
        self.volume_slider.setToolTip("预览监听音量")
        controls.addWidget(self.prev_button)
        controls.addWidget(self.play_button)
        controls.addWidget(self.next_button)
        controls.addWidget(self.stop_button)
        controls.addSpacing(4)
        controls.addWidget(self.time_label)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.mode_label)
        controls.addWidget(self.loop_label)
        controls.addSpacing(4)
        controls.addWidget(self.volume_slider)
        controls.addWidget(self.mute_button)
        layout.addLayout(controls)

        self._apply_button_icons()
        self._set_transport_enabled(False)
        self.play_button.clicked.connect(self.toggle_play)
        self.prev_button.clicked.connect(lambda: self.step_requested.emit(-1))
        self.next_button.clicked.connect(lambda: self.step_requested.emit(1))
        self.stop_button.clicked.connect(self.stop)
        self.slider.sliderMoved.connect(self._on_slider_moved)
        self.mute_button.clicked.connect(self.toggle_mute)
        self.volume_slider.valueChanged.connect(self.set_volume)

    def _set_transport_enabled(self, enabled: bool) -> None:
        for button in (
            self.prev_button,
            self.play_button,
            self.next_button,
            self.stop_button,
        ):
            button.setEnabled(enabled)

    def _apply_button_icons(self) -> None:
        apply_standard_icon(
            self.play_button, QStyle.StandardPixmap.SP_MediaPlay, 16
        )
        apply_standard_icon(
            self.prev_button,
            QStyle.StandardPixmap.SP_MediaSkipBackward,
            16,
        )
        apply_standard_icon(
            self.next_button,
            QStyle.StandardPixmap.SP_MediaSkipForward,
            16,
        )
        apply_standard_icon(
            self.stop_button, QStyle.StandardPixmap.SP_MediaStop, 16
        )
        self._update_volume_button()

    def _update_volume_button(self) -> None:
        icon = (
            QStyle.StandardPixmap.SP_MediaVolumeMuted
            if self._muted
            else QStyle.StandardPixmap.SP_MediaVolume
        )
        apply_standard_icon(self.mute_button, icon, 16)
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
        self._set_transport_enabled(False)
        self.slider.setRange(0, 0)
        self.slider.setValue(0)
        self.time_label.setText("加载中… / 00:00.000")
        self.mode_label.setText(mode)
        self.mode_history.append(str(mode))
        self.placeholder.setText("")
        self.placeholder.hide()
        if self.video is not None:
            self.video.show()

    def is_playing(self) -> bool:
        return (
            self.player is not None
            and self.player.playbackState()
            == QMediaPlayer.PlaybackState.PlayingState
        )

    def clear(self) -> None:
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())
        self.slider.setRange(0, 0)
        self._set_transport_enabled(False)
        self.mode_label.setText("")
        self.pip_overlay.set_pips([])
        self.pip_overlay.hide()
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

    def set_pips(self, pips: list[dict] | None) -> None:
        self.pip_overlay.set_pips(pips)
        if pips:
            self.pip_overlay.setGeometry(self.video_frame.rect())
            self.pip_overlay.show()
            self.pip_overlay.raise_()
        else:
            self.pip_overlay.hide()

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
        self.pip_overlay.setGeometry(self.video_frame.rect())
        if self.pip_overlay.pips:
            self.pip_overlay.raise_()
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
        position = self.player.position() if self.player is not None else 0
        self.time_label.setText(
            f"{_format_ms(position)} / {_format_ms(duration_ms)}"
        )
        self._set_transport_enabled(duration_ms > 0)

    def _on_state(self, state) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        icon = (
            QStyle.StandardPixmap.SP_MediaPause
            if playing
            else QStyle.StandardPixmap.SP_MediaPlay
        )
        apply_standard_icon(self.play_button, icon, 16)
        self.play_button.setText("暂停" if playing else "播放")
        self.playing_changed.emit(playing)

    def _on_error(self, _error, message: str) -> None:
        if message:
            self._set_transport_enabled(False)
            self.placeholder.setText(f"播放失败: {message}")
            self.placeholder.show()
            self.mode_label.setText("预览错误")


def _format_ms(value: int) -> str:
    if value <= 0:
        return "00:00.000"
    total_seconds = value / 1000.0
    minutes = int(total_seconds // 60)
    seconds = total_seconds - minutes * 60
    return f"{minutes:02d}:{seconds:06.3f}"
