"""自绘时间线：缩放/平移、标尺、波形、缩略图、片段拖拽与 diff 高亮。"""

from __future__ import annotations

import math
from copy import deepcopy

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from smartcut.desktop.formatting import seconds_or_none

MIN_SEGMENT = 0.1
EDGE_PX = 6.0
MAX_ZOOM = 50.0


class TimelineWidget(QWidget):
    seek_requested = Signal(float)
    segment_selected = Signal(int, object)
    selection_cleared = Signal()
    segments_edited = Signal(object)
    view_changed = Signal(float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.duration = 0.0
        self.segments: list[dict] = []
        self.shots: list[dict] = []
        self.quality_marks: list[dict] = []
        self.evidence: list[dict] = []
        self.position = 0.0
        self.zoom = 1.0
        self.view_start = 0.0
        self.thumbnails: list[tuple[float, QPixmap]] = []
        self.waveform: list[float] = []
        self.audio_tracks: list[dict] = []
        self.audio_waveforms: dict[str, list[float]] = {}
        self.effect_segments: set[str] = set()
        self._selected = -1
        self._drag: dict | None = None
        self._pan: dict | None = None
        self._diff_added: set[tuple[float, float]] = set()
        self._diff_removed: set[tuple[float, float]] = set()
        self._diff_unchanged: set[tuple[float, float]] = set()
        self._diff_active = False
        self.setMinimumHeight(150)
        self.setMouseTracking(True)
        self.setToolTip("滚轮缩放；Shift+滚轮或中键拖动平移；拖动片段移动，拖动边缘裁剪")

    @property
    def visible_duration(self) -> float:
        return self.duration / max(self.zoom, 1.0) if self.duration > 0 else 0.0

    @property
    def view_end(self) -> float:
        return min(self.duration, self.view_start + self.visible_duration)

    def set_data(
        self,
        duration: float,
        segments: list[dict],
        position: float | None = None,
    ) -> None:
        previous_duration = self.duration
        self.duration = max(float(duration), 0.0)
        self.segments = [deepcopy(segment) for segment in segments]
        if self.duration != previous_duration:
            self.zoom = 1.0
            self.view_start = 0.0
        if position is not None:
            self.position = max(float(position), 0.0)
        if self._selected >= len(self.segments):
            self._selected = -1
        self._clamp_view()
        self.update()

    def set_effect_segments(self, segment_ids: set[str] | list[str] | None) -> None:
        self.effect_segments = set(segment_ids or ())
        self.update()

    def set_position(self, seconds: float) -> None:
        self.position = max(float(seconds), 0.0)
        if self.duration > 0:
            visible = self.visible_duration
            if self.position < self.view_start or self.position > self.view_end:
                self.view_start = max(
                    0.0,
                    min(self.duration - visible, self.position - visible / 2),
                )
                self.view_changed.emit(self.view_start, visible)
        self.update()

    def set_overlays(self, shots: list[dict], quality_marks: list[dict]) -> None:
        self.shots = shots
        self.quality_marks = quality_marks
        self.update()

    def set_evidence(self, items: list[dict] | None) -> None:
        self.evidence = list(items or [])
        self.update()

    def set_thumbnails(self, thumbnails: list[tuple[float, QPixmap]]) -> None:
        self.thumbnails = list(thumbnails)
        self.update()

    def set_waveform(self, peaks: list[float]) -> None:
        self.waveform = [max(0.0, min(1.0, float(value))) for value in peaks]
        self.update()

    def set_audio_tracks(self, tracks: list[dict] | None) -> None:
        self.audio_tracks = [_normalize_audio_track(track) for track in tracks or []]
        self.update()

    def set_audio_waveforms(self, payload: dict | None) -> None:
        payload = payload or {}
        waveforms: dict[str, list[float]] = {"main": list(payload.get("main") or [])}
        for asset_id, peaks in (payload.get("assets") or {}).items():
            waveforms[str(asset_id)] = list(peaks or [])
        self.audio_waveforms = waveforms
        # 同一次 build_audio_waveforms 已包含主素材 peaks，复用它可避免
        # 额外再跑一次 build_waveform（Windows 上还会与缓存原子替换竞争）。
        if waveforms["main"]:
            self.waveform = [
                max(0.0, min(1.0, float(value))) for value in waveforms["main"]
            ]
        self.update()

    def set_diff(self, added, removed, unchanged) -> None:
        def intervals(items) -> set[tuple[float, float]]:
            result = set()
            for item in items or []:
                try:
                    result.add(
                        (
                            round(float(item["start_seconds"]), 3),
                            round(float(item["end_seconds"]), 3),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            return result

        self._diff_added = intervals(added)
        self._diff_removed = intervals(removed)
        self._diff_unchanged = intervals(unchanged)
        self._diff_active = bool(
            self._diff_added or self._diff_removed or self._diff_unchanged
        )
        self.update()

    def clear_diff(self) -> None:
        self._diff_added.clear()
        self._diff_removed.clear()
        self._diff_unchanged.clear()
        self._diff_active = False
        self.update()

    def select_segment(self, index: int) -> None:
        self._selected = index
        self.update()

    def fit(self) -> None:
        self.zoom = 1.0
        self.view_start = 0.0
        self.view_changed.emit(self.view_start, self.visible_duration)
        self.update()

    def zoom_in(self) -> None:
        self.zoom_by(1.4, self.position)

    def zoom_out(self) -> None:
        self.zoom_by(1 / 1.4, self.position)

    def zoom_by(self, factor: float, anchor_time: float | None = None) -> None:
        if self.duration <= 0:
            return
        anchor = self.position if anchor_time is None else float(anchor_time)
        old_visible = self.visible_duration
        ratio = (
            (anchor - self.view_start) / old_visible
            if old_visible > 0
            else 0.5
        )
        self.zoom = max(1.0, min(MAX_ZOOM, self.zoom * float(factor)))
        new_visible = self.visible_duration
        self.view_start = max(
            0.0,
            min(self.duration - new_visible, anchor - new_visible * ratio),
        )
        self._clamp_view()
        self.view_changed.emit(self.view_start, self.visible_duration)
        self.update()

    def pan_by(self, seconds: float) -> None:
        self.view_start += float(seconds)
        self._clamp_view()
        self.view_changed.emit(self.view_start, self.visible_duration)
        self.update()

    def _clamp_view(self) -> None:
        if self.duration <= 0:
            self.zoom = 1.0
            self.view_start = 0.0
            return
        visible = min(self.duration, self.duration / max(self.zoom, 1.0))
        self.view_start = max(0.0, min(self.duration - visible, self.view_start))

    def _inner(self) -> QRectF:
        return QRectF(self.rect().adjusted(14, 10, -14, -10))

    def _lanes_full(self) -> tuple[QRectF, QRectF, QRectF, QRectF, QRectF]:
        inner = self._inner()
        ruler = QRectF(inner.left(), inner.top(), inner.width(), 16)
        thumbs = QRectF(inner.left(), ruler.bottom() + 2, inner.width(), 32)
        wave = QRectF(inner.left(), thumbs.bottom() + 2, inner.width(), 22)
        audio = QRectF(inner.left(), wave.bottom() + 2, inner.width(), 38)
        segments = QRectF(
            inner.left(),
            audio.bottom() + 4,
            inner.width(),
            max(30, inner.bottom() - audio.bottom() - 4),
        )
        return ruler, thumbs, wave, audio, segments

    def _lanes(self) -> tuple[QRectF, QRectF, QRectF, QRectF]:
        ruler, thumbs, wave, _audio, segments = self._lanes_full()
        return ruler, thumbs, wave, segments

    def _time_to_x(self, seconds: float, lane: QRectF) -> float:
        visible = self.visible_duration
        if visible <= 0:
            return lane.left()
        return lane.left() + (seconds - self.view_start) / visible * lane.width()

    def _x_to_time(self, x: float, lane: QRectF) -> float:
        if lane.width() <= 0:
            return 0.0
        return self.view_start + (x - lane.left()) / lane.width() * self.visible_duration

    def sizeHint(self) -> QSize:
        return QSize(700, 190)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect().adjusted(6, 6, -6, -6))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#111827"))
        painter.drawRoundedRect(rect, 8, 8)
        if self.duration <= 0:
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "暂无时间线")
            return

        ruler, thumbs, wave, audio, segments = self._lanes_full()
        self._paint_ruler(painter, ruler)
        self._paint_evidence(painter, ruler)
        self._paint_thumbnails(painter, thumbs)
        self._paint_waveform(painter, wave)
        self._paint_audio(painter, audio)
        self._paint_segments(painter, segments)
        self._paint_overlays(painter, ruler, segments)
        self._paint_playhead(painter, ruler, segments)

    def _paint_ruler(self, painter: QPainter, ruler: QRectF) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#0b1220"))
        painter.drawRoundedRect(ruler, 3, 3)
        visible = self.visible_duration
        target = visible / 8.0
        interval = 1.0
        for candidate in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1200, 3600):
            interval = candidate
            if candidate >= target:
                break
        first = math.ceil(self.view_start / interval) * interval
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        value = first
        while value <= self.view_end + 1e-6:
            x = self._time_to_x(value, ruler)
            painter.setPen(QPen(QColor("#334155"), 1))
            painter.drawLine(x, ruler.top(), x, ruler.bottom())
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(
                QRectF(x + 3, ruler.top() - 1, 70, 15),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                _format_time(value, interval),
            )
            value += interval

    def _paint_evidence(self, painter: QPainter, lane: QRectF) -> None:
        if not self.evidence or self.duration <= 0:
            return
        colors = {
            "tile": QColor("#38bdf8"),
            "silence": QColor("#f59e0b"),
            "candidate": QColor("#a78bfa"),
        }
        font = QFont()
        font.setPointSize(7)
        painter.setFont(font)
        for item in self.evidence:
            try:
                start = float(item.get("start"))
                end = float(item.get("end"))
            except (TypeError, ValueError):
                continue
            if end <= self.view_start or start >= self.view_end:
                continue
            x1 = self._time_to_x(start, lane)
            x2 = self._time_to_x(end, lane)
            if x2 - x1 < 1.0:
                x2 = x1 + 1.0
            kind = str(item.get("kind") or "tile")
            painter.fillRect(
                QRectF(x1, lane.top() + 2, max(1.0, x2 - x1), 3),
                colors.get(kind, colors["tile"]),
            )
            label = str(item.get("label") or "")
            if label and kind == "tile" and x2 - x1 > 18:
                painter.setPen(colors.get(kind, colors["tile"]))
                painter.drawText(
                    QRectF(x1 + 1, lane.top() + 6, 40, 12),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    label,
                )

    def _paint_thumbnails(self, painter: QPainter, lane: QRectF) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#0b1220"))
        painter.drawRoundedRect(lane, 3, 3)
        height = lane.height()
        width = height * 16 / 9
        for seconds, pixmap in self.thumbnails:
            if seconds < self.view_start or seconds > self.view_end:
                continue
            x = self._time_to_x(seconds, lane)
            painter.drawPixmap(QRectF(x, lane.top(), width, height).toRect(), pixmap)

    def _paint_waveform(self, painter: QPainter, lane: QRectF) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#0b1220"))
        painter.drawRoundedRect(lane, 3, 3)
        painter.setPen(QPen(QColor("#334155"), 1))
        center = lane.center().y()
        painter.drawLine(lane.left(), center, lane.right(), center)
        if not self.waveform or self.duration <= 0:
            return
        painter.setPen(QPen(QColor("#38bdf8"), 1))
        half = lane.height() / 2 - 2
        for x in range(int(lane.left()), int(lane.right()) + 1):
            time_value = self._x_to_time(x, lane)
            index = int(time_value / self.duration * len(self.waveform))
            if 0 <= index < len(self.waveform):
                amplitude = self.waveform[index] * half
                if amplitude > 0.2:
                    painter.drawLine(x, center - amplitude, x, center + amplitude)

    def _paint_audio(self, painter: QPainter, lane: QRectF) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#0b1220"))
        painter.drawRoundedRect(lane, 3, 3)
        if not self.audio_tracks:
            painter.setPen(QColor("#475569"))
            font = QFont()
            font.setPointSize(8)
            painter.setFont(font)
            painter.drawText(
                lane.adjusted(6, 0, -6, 0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                "音频 lane（无音轨）",
            )
            return

        rows = min(len(self.audio_tracks), 4)
        row_height = max(8.0, (lane.height() - 2) / max(1, rows))
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        for index, track in enumerate(self.audio_tracks[:4]):
            start = track["timeline_start"]
            if track["loop"]:
                end = self.duration
            else:
                end = start + max(0.05, track["duration"])
            end = min(end, self.duration)
            if end <= self.view_start or start > self.view_end:
                continue
            x1 = self._time_to_x(max(start, self.view_start), lane)
            x2 = self._time_to_x(min(end, self.view_end), lane)
            width = max(2.0, x2 - x1)
            rect = QRectF(
                x1,
                lane.top() + 1 + index * row_height,
                width,
                row_height - 2,
            )
            if track["muted"]:
                color = QColor("#475569")
            elif track["solo"]:
                color = QColor("#d97706")
            else:
                color = {
                    "main": QColor("#2563eb"),
                    "music": QColor("#0d9488"),
                    "voice": QColor("#7c3aed"),
                    "stem_vocals": QColor("#db2777"),
                    "stem_instrumental": QColor("#0891b2"),
                }.get(track["role"], QColor("#475569"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(rect, 3, 3)
            self._paint_audio_waveform(painter, rect, track)

            label = track["name"] or track["asset_id"]
            if rect.width() > 36:
                painter.setPen(QColor("#f8fafc"))
                painter.drawText(
                    rect.adjusted(4, 0, -4, 0),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    label[:20],
                )

    def _paint_audio_waveform(
        self,
        painter: QPainter,
        rect: QRectF,
        track: dict,
    ) -> None:
        peaks = self.audio_waveforms.get(track["asset_id"]) or []
        if not peaks or rect.width() < 3:
            return
        span = max(0.001, track["source_end"] - track["source_start"])
        center = rect.center().y()
        half = max(1.0, rect.height() / 2 - 2)
        painter.setPen(QPen(QColor(248, 250, 252, 150), 1))
        for x in range(int(rect.left()) + 1, int(rect.right())):
            ratio = (x - rect.left()) / max(1.0, rect.width())
            if track["loop"]:
                source_time = track["source_start"] + (
                    ratio * max(0.05, track["duration"])
                ) % span
            else:
                source_time = track["source_start"] + ratio * span
            index = int((source_time - track["source_start"]) / span * len(peaks))
            index = max(0, min(len(peaks) - 1, index))
            amplitude = peaks[index] * half
            if amplitude > 0.2:
                painter.drawLine(x, center - amplitude, x, center + amplitude)

    def _segment_rect(self, segment: dict, lane: QRectF) -> QRectF | None:
        try:
            start = max(float(segment["start"]), 0.0)
            end = min(float(segment["end"]), self.duration)
        except (KeyError, TypeError, ValueError):
            return None
        if end <= start or end < self.view_start or start > self.view_end:
            return None
        x1 = self._time_to_x(max(start, self.view_start), lane)
        x2 = self._time_to_x(min(end, self.view_end), lane)
        return QRectF(x1, lane.top(), max(2.0, x2 - x1), lane.height())

    def _paint_segments(self, painter: QPainter, lane: QRectF) -> None:
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        for index, segment in enumerate(self.segments):
            segment_rect = self._segment_rect(segment, lane)
            if segment_rect is None:
                continue
            confidence = segment.get("confidence")
            key = (
                round(float(segment["start"]), 3),
                round(float(segment["end"]), 3),
            )
            if self._diff_active:
                if key in self._diff_added:
                    color = QColor("#16a34a")
                elif key in self._diff_unchanged:
                    color = QColor("#2563eb")
                else:
                    color = QColor("#64748b")
            else:
                color = (
                    QColor("#f59e0b")
                    if confidence is not None and confidence < 0.6
                    else QColor("#2563eb")
                )
            painter.setBrush(color)
            painter.setPen(
                QPen(QColor("#f8fafc"), 2) if index == self._selected else Qt.PenStyle.NoPen
            )
            painter.drawRoundedRect(segment_rect, 4, 4)
            tag = str(segment.get("tag") or "")
            if tag and segment_rect.width() > 42:
                painter.setPen(QColor("#f8fafc"))
                painter.drawText(
                    segment_rect.adjusted(6, 0, -6, 0),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    tag[:24],
                )
            segment_id = segment.get("id")
            if (
                segment_id
                and segment_id in self.effect_segments
                and segment_rect.width() > 26
            ):
                badge = QRectF(
                    segment_rect.right() - 24,
                    segment_rect.top() + 2,
                    22,
                    12,
                )
                painter.save()
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#0ea5e9"))
                painter.drawRoundedRect(badge, 3, 3)
                badge_font = QFont()
                badge_font.setPointSize(7)
                badge_font.setBold(True)
                painter.setFont(badge_font)
                painter.setPen(QColor("#0b1220"))
                painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, "FX")
                painter.restore()

        if self._diff_active:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(239, 68, 68, 180))
            for start, end in sorted(self._diff_removed):
                if end < self.view_start or start > self.view_end:
                    continue
                x1 = self._time_to_x(max(0.0, start), lane)
                x2 = self._time_to_x(min(self.duration, end), lane)
                painter.drawRoundedRect(
                    QRectF(x1, lane.bottom() - 7, max(2.0, x2 - x1), 5),
                    2,
                    2,
                )

    def _paint_overlays(
        self,
        painter: QPainter,
        ruler: QRectF,
        segments: QRectF,
    ) -> None:
        visible_shots = [
            value
            for value in (seconds_or_none(shot.get("t")) for shot in self.shots)
            if value is not None and self.view_start <= value <= self.view_end
        ]
        step = max(1, len(visible_shots) // 200)
        painter.setPen(QPen(QColor("#f59e0b"), 1, Qt.PenStyle.DashLine))
        for seconds in visible_shots[::step]:
            x = self._time_to_x(seconds, segments)
            painter.drawLine(x, ruler.bottom(), x, segments.bottom())

        visible_marks = [
            value
            for value in (seconds_or_none(mark.get("t")) for mark in self.quality_marks)
            if value is not None and self.view_start <= value <= self.view_end
        ]
        step = max(1, len(visible_marks) // 200)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ef4444"))
        for seconds in visible_marks[::step]:
            x = self._time_to_x(seconds, segments)
            painter.drawEllipse(QRectF(x - 2.5, segments.bottom() - 5, 5, 5))

    def _paint_playhead(
        self,
        painter: QPainter,
        ruler: QRectF,
        segments: QRectF,
    ) -> None:
        if not (self.view_start <= self.position <= self.view_end):
            return
        x = self._time_to_x(self.position, segments)
        painter.setPen(QPen(QColor("#ef4444"), 2))
        painter.drawLine(x, ruler.top(), x, segments.bottom())

    def _hit(self, x: float, y: float) -> tuple[int, str] | None:
        _ruler, _thumbs, _wave, lane = self._lanes()
        for index in range(len(self.segments) - 1, -1, -1):
            rect = self._segment_rect(self.segments[index], lane)
            if rect is None or not rect.adjusted(0, -4, 0, 4).contains(x, y):
                continue
            if x - rect.left() <= EDGE_PX:
                return index, "left"
            if rect.right() - x <= EDGE_PX:
                return index, "right"
            return index, "move"
        return None

    def wheelEvent(self, event) -> None:
        if self.duration <= 0:
            return
        _ruler, _thumbs, _wave, lane = self._lanes()
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.pan_by(-event.angleDelta().y() / 120 * self.visible_duration * 0.1)
        else:
            anchor = self._x_to_time(event.position().x(), lane)
            factor = 1.25 if event.angleDelta().y() > 0 else 1 / 1.25
            self.zoom_by(factor, anchor)
        event.accept()

    def mousePressEvent(self, event) -> None:
        if self.duration <= 0:
            return
        _ruler, _thumbs, _wave, lane = self._lanes()
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan = {"x": event.position().x(), "start": self.view_start}
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        hit = self._hit(event.position().x(), event.position().y())
        if hit is None:
            self._selected = -1
            self.update()
            self.selection_cleared.emit()
            self._emit_seek(event.position().x(), lane)
            return
        index, zone = hit
        self._selected = index
        self.update()
        segment = deepcopy(self.segments[index])
        self.segment_selected.emit(index, segment)
        self._drag = {
            "index": index,
            "zone": zone,
            "x": event.position().x(),
            "start": float(segment["start"]),
            "end": float(segment["end"]),
            "moved": False,
        }
        self._emit_seek(event.position().x(), lane)

    def mouseMoveEvent(self, event) -> None:
        if self.duration <= 0:
            return
        _ruler, _thumbs, _wave, lane = self._lanes()
        if self._pan is not None:
            delta = (
                (event.position().x() - self._pan["x"])
                / lane.width()
                * self.visible_duration
            )
            self.view_start = self._pan["start"] - delta
            self._clamp_view()
            self.view_changed.emit(self.view_start, self.visible_duration)
            self.update()
            return
        if self._drag is None:
            hit = self._hit(event.position().x(), event.position().y())
            if hit is None:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            elif hit[1] in {"left", "right"}:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            else:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            return
        if lane.width() <= 0:
            return
        delta = (
            (event.position().x() - self._drag["x"]) / lane.width() * self.visible_duration
        )
        if abs(delta) < 0.001:
            return
        self._drag["moved"] = True
        index = self._drag["index"]
        original_start = self._drag["start"]
        original_end = self._drag["end"]
        zone = self._drag["zone"]
        if zone == "move":
            length = original_end - original_start
            start = max(0.0, min(original_start + delta, self.duration - length))
            end = start + length
        elif zone == "left":
            start = max(0.0, min(original_start + delta, original_end - MIN_SEGMENT))
            end = original_end
        else:
            start = original_start
            end = min(
                self.duration,
                max(original_end + delta, original_start + MIN_SEGMENT),
            )
        self.segments[index] = {
            **self.segments[index],
            "start": round(start, 6),
            "end": round(end, 6),
        }
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._pan is not None:
            self._pan = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        if self._drag is None:
            return
        moved = bool(self._drag.get("moved"))
        self._drag = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if moved:
            self.segments_edited.emit([deepcopy(item) for item in self.segments])

    def _emit_seek(self, x: float, lane: QRectF) -> None:
        self.seek_requested.emit(
            max(0.0, min(self.duration, self._x_to_time(x, lane)))
        )


def _normalize_audio_track(track: dict) -> dict:
    timeline_start = seconds_or_none(track.get("timeline_start")) or 0.0
    source_start = seconds_or_none(track.get("source_start")) or 0.0
    source_end = seconds_or_none(track.get("source_end"))
    duration = (
        max(0.05, source_end - source_start)
        if source_end is not None and source_end > source_start
        else max(0.05, float(track.get("duration") or 0.0))
    )
    return {
        "id": str(track.get("id") or ""),
        "name": str(track.get("name") or ""),
        "asset_id": str(track.get("asset_id") or "main"),
        "role": str(track.get("role") or "other"),
        "timeline_start": max(0.0, timeline_start),
        "source_start": max(0.0, source_start),
        "source_end": source_end if source_end is not None else source_start + duration,
        "duration": duration,
        "muted": bool(track.get("muted")),
        "solo": bool(track.get("solo")),
        "loop": bool(track.get("loop")),
        "gain_db": float(track.get("gain_db") or 0.0),
    }


def _format_time(seconds: float, interval: float) -> str:
    if abs(seconds - round(seconds)) < 1e-6:
        seconds = round(seconds)
    if interval >= 60 or seconds >= 60:
        minutes = int(seconds // 60)
        rest = seconds - minutes * 60
        return f"{minutes}:{rest:04.1f}" if interval < 10 else f"{minutes}:{int(rest):02d}"
    return f"{seconds:.1f}" if interval < 1 else f"{int(seconds)}"
