"""自绘时间线：缩放/平移、标尺、波形、缩略图、片段拖拽与 diff 高亮。"""

from __future__ import annotations

import json
import math
from copy import deepcopy

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from smartcut.contracts.timeline_ops import TRANSITION_TYPES
from smartcut.desktop.formatting import seconds_or_none
from smartcut.desktop.theme import NLE

_TRANSITION_TYPES = tuple(TRANSITION_TYPES)

MIN_SEGMENT = 0.1
EDGE_PX = 6.0
MAX_ZOOM = 50.0


class TimelineWidget(QWidget):
    seek_requested = Signal(float)
    segment_selected = Signal(int, object)
    selection_cleared = Signal()
    segments_edited = Signal(object)
    asset_dropped = Signal(object, float, int)
    transition_changed = Signal(str, float, str)
    track_state_changed = Signal(int, str, bool)
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
        self.source_durations: dict[str, float] = {}
        self.track_states: dict[int, dict] = {}
        self.transitions: list[dict] = []
        self.pips: list[dict] = []
        self.output_time_enabled = False
        self.default_transition_type = "fade"
        self._transition_preview: dict | None = None
        self.audio_tracks: list[dict] = []
        self.audio_waveforms: dict[str, list[float]] = {}
        self.effect_segments: set[str] = set()
        self._selected = -1
        self._drag: dict | None = None
        self._pan: dict | None = None
        self._snap_line: float | None = None
        self._diff_added: set[tuple[float, float]] = set()
        self._diff_removed: set[tuple[float, float]] = set()
        self._diff_unchanged: set[tuple[float, float]] = set()
        self._diff_active = False
        self.setMinimumHeight(150)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setToolTip(
            "滚轮缩放；Shift+滚轮或中键拖动平移；拖动片段移动，拖动边缘裁剪；"
            "从素材 bin 拖入可追加片段"
        )

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
        self.segments = [deepcopy(segment) for segment in segments]
        self.duration = max(float(duration), 0.0)
        if self.output_time_enabled:
            _, total = self._output_spans()
            self.duration = total
        if self.duration != previous_duration:
            self.zoom = 1.0
            self.view_start = 0.0
        if position is not None:
            self.position = (
                self._source_to_output(float(position))
                if self.output_time_enabled
                else max(float(position), 0.0)
            )
        if self._selected >= len(self.segments):
            self._selected = -1
        self._clamp_view()
        self.update()

    def set_effect_segments(self, segment_ids: set[str] | list[str] | None) -> None:
        self.effect_segments = set(segment_ids or ())
        self.update()

    def set_position(self, seconds: float) -> None:
        self.position = (
            self._source_to_output(float(seconds))
            if self.output_time_enabled
            else max(float(seconds), 0.0)
        )
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

    def set_output_time(self, enabled: bool) -> None:
        self.output_time_enabled = bool(enabled)
        self.update()

    def _speed(self, segment: dict) -> float:
        try:
            return max(0.5, min(2.0, float(segment.get("speed") or 1.0)))
        except (TypeError, ValueError):
            return 1.0

    def _output_spans(self) -> tuple[list[tuple[float, float]], float]:
        if not self.output_time_enabled:
            spans: list[tuple[float, float]] = []
            for segment in self.segments:
                try:
                    spans.append(
                        (float(segment["start"]), float(segment["end"]))
                    )
                except (KeyError, TypeError, ValueError):
                    spans.append((0.0, 0.0))
            return spans, self.duration
        transition_by_after = {
            str(item.get("after")): self._duration_seconds(
                item.get("duration")
            )
            for item in self.transitions
        }
        absolute = any(
            segment.get("timeline_start") is not None
            for segment in self.segments
        )
        spans = []
        cursor = 0.0
        for segment in self.segments:
            try:
                duration = max(
                    0.0,
                    (float(segment["end"]) - float(segment["start"]))
                    / self._speed(segment),
                )
            except (KeyError, TypeError, ValueError):
                duration = 0.0
            explicit = segment.get("timeline_start")
            if absolute and explicit is not None:
                start = max(0.0, float(explicit))
                end = start + duration
            else:
                start = cursor
                end = start + duration
            spans.append((start, end))
            cursor = max(0.0, end - transition_by_after.get(str(segment.get("id")), 0.0))
        if absolute:
            cursor = max((end for _, end in spans), default=0.0)
        return spans, cursor

    def _span_for(self, segment: dict) -> tuple[float, float]:
        spans, _ = self._output_spans()
        for index, item in enumerate(self.segments):
            if item is segment:
                return spans[index]
        try:
            index = self.segments.index(segment)
            return spans[index]
        except (ValueError, IndexError):
            return (0.0, 0.0)

    def _source_to_output(self, seconds: float) -> float:
        if not self.output_time_enabled:
            return max(0.0, float(seconds))
        spans, _ = self._output_spans()
        for index, segment in enumerate(self.segments):
            try:
                start = float(segment["start"])
                end = float(segment["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if start <= seconds <= end:
                span_start = spans[index][0]
                return span_start + (seconds - start) / self._speed(segment)
        if spans:
            return max(0.0, min(self.duration, spans[-1][1]))
        return max(0.0, float(seconds))

    def _output_to_source(self, seconds: float) -> float:
        if not self.output_time_enabled:
            return max(0.0, float(seconds))
        spans, _ = self._output_spans()
        for index, segment in enumerate(self.segments):
            span_start, span_end = spans[index]
            if span_start <= seconds <= span_end:
                try:
                    source_start = float(segment["start"])
                except (KeyError, TypeError, ValueError):
                    return span_start
                return source_start + (seconds - span_start) * self._speed(segment)
        if self.segments:
            try:
                return float(self.segments[-1].get("end") or 0.0)
            except (TypeError, ValueError):
                return 0.0
        return max(0.0, float(seconds))

    def set_transitions(self, transitions: list[dict] | None) -> None:
        self.transitions = [dict(item) for item in transitions or []]
        self.update()

    def set_track_states(self, states: dict | None) -> None:
        self.track_states = {
            int(track): dict(value)
            for track, value in (states or {}).items()
        }
        self.update()

    def _header_hit(self, x: float, y: float) -> tuple[int, str] | None:
        for track, lane in enumerate(self._track_lanes()):
            if not (lane.top() <= y <= lane.bottom()):
                continue
            if lane.left() + 24 <= x <= lane.left() + 24 + 3 * 18:
                field = ("muted", "solo", "locked")[
                    min(2, int((x - lane.left() - 24) // 18))
                ]
                return track, field
        return None

    def set_pips(self, pips: list[dict] | None) -> None:
        self.pips = [dict(item) for item in pips or []]
        self.update()

    def _paint_pips(self, painter: QPainter, lane: QRectF) -> None:
        for pip in self.pips:
            start = self._duration_seconds(pip.get("timeline_start"))
            duration = self._duration_seconds(pip.get("duration"))
            if duration <= 0:
                continue
            x1 = self._time_to_x(start, lane)
            x2 = self._time_to_x(start + duration, lane)
            rect = QRectF(x1, lane.top() + 1, max(4.0, x2 - x1), 9)
            painter.setPen(QPen(QColor("#22d3ee"), 1))
            painter.setBrush(QColor(34, 211, 238, 60))
            painter.drawRoundedRect(rect, 3, 3)
            if rect.width() > 28:
                painter.setPen(QColor("#a5f3fc"))
                painter.drawText(
                    rect.adjusted(3, 0, -3, 0),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    "PiP",
                )

    @staticmethod
    def _duration_seconds(value) -> float:
        if isinstance(value, dict):
            try:
                return float(value.get("value", 0)) / float(
                    value.get("rate", 1)
                )
            except (TypeError, ValueError, ZeroDivisionError):
                return 0.0
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def _transition_for(self, segment_id: str) -> dict | None:
        return next(
            (
                item
                for item in self.transitions
                if str(item.get("after")) == segment_id
            ),
            None,
        )

    def set_source_durations(self, durations: dict[str, float]) -> None:
        self.source_durations = {
            str(key): float(value)
            for key, value in (durations or {}).items()
            if value is not None and float(value) > 0
        }

    def _limit_for(self, segment: dict | None) -> float:
        src = str((segment or {}).get("src") or "")
        return self.source_durations.get(src, self.duration)

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

    def _track_count(self) -> int:
        return max(1, max((int(s.get("track") or 0) for s in self.segments), default=0) + 1)

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

    def _track_lanes(self) -> list[QRectF]:
        _ruler, _thumbs, _wave, _audio, area = self._lanes_full()
        count = self._track_count()
        lane_height = max(30.0, area.height() / count)
        return [
            QRectF(
                area.left(),
                area.bottom() - (track + 1) * lane_height,
                area.width(),
                lane_height,
            )
            for track in range(count)
        ]

    def _lane_for_track(self, track: int) -> QRectF:
        lanes = self._track_lanes()
        index = max(0, min(int(track), len(lanes) - 1))
        return lanes[index]

    def _track_at_y(self, y: float) -> int:
        for track, lane in enumerate(self._track_lanes()):
            if lane.top() <= y <= lane.bottom():
                return track
        return 0

    def _lanes(self) -> tuple[QRectF, QRectF, QRectF, QRectF]:
        ruler, thumbs, wave, _audio, _segments = self._lanes_full()
        return ruler, thumbs, wave, self._lane_for_track(0)

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
        return QSize(700, 190 + 30 * (self._track_count() - 1))

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect().adjusted(6, 6, -6, -6))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(NLE["canvas"]))
        painter.drawRoundedRect(rect, 8, 8)
        if self.duration <= 0:
            painter.setPen(QColor(NLE["text"]))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "暂无时间线")
            return

        ruler, thumbs, wave, audio, segments = self._lanes_full()
        self._paint_ruler(painter, ruler)
        self._paint_evidence(painter, ruler)
        self._paint_thumbnails(painter, thumbs)
        self._paint_waveform(painter, wave)
        self._paint_audio(painter, audio)
        for track, lane in enumerate(self._track_lanes()):
            self._paint_segments(painter, lane, track)
        self._paint_pips(painter, self._lane_for_track(0))
        self._paint_transitions(painter, self._lane_for_track(0))
        self._paint_overlays(painter, ruler, segments)
        self._paint_snap_line(painter, ruler, segments)
        self._paint_playhead(painter, ruler, segments)

    def _paint_ruler(self, painter: QPainter, ruler: QRectF) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(NLE["lane"]))
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
            painter.setPen(QPen(QColor(NLE["grid"]), 1))
            painter.drawLine(x, ruler.top(), x, ruler.bottom())
            painter.setPen(QColor(NLE["text"]))
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
            "silence": QColor(NLE["solo"]),
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
        painter.setBrush(QColor(NLE["lane"]))
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
        painter.setBrush(QColor(NLE["lane"]))
        painter.drawRoundedRect(lane, 3, 3)
        painter.setPen(QPen(QColor(NLE["grid"]), 1))
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
        painter.setBrush(QColor(NLE["lane"]))
        painter.drawRoundedRect(lane, 3, 3)
        if not self.audio_tracks:
            painter.setPen(QColor(NLE["muted"]))
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
                color = QColor(NLE["muted"])
            elif track["solo"]:
                color = QColor(NLE["solo"])
            else:
                color = QColor(
                    NLE["audio_palette"].get(track["role"], NLE["muted"])
                )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(rect, 3, 3)
            self._paint_audio_waveform(painter, rect, track)

            label = track["name"] or track["asset_id"]
            if rect.width() > 36:
                painter.setPen(QColor(NLE["white"]))
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
            if self.output_time_enabled:
                start, end = self._span_for(segment)
                start = max(start, 0.0)
                end = min(end, self.duration)
            else:
                start = max(float(segment["start"]), 0.0)
                end = min(float(segment["end"]), self.duration)
        except (KeyError, TypeError, ValueError):
            return None
        if end <= start or end < self.view_start or start > self.view_end:
            return None
        x1 = self._time_to_x(max(start, self.view_start), lane)
        x2 = self._time_to_x(min(end, self.view_end), lane)
        return QRectF(x1, lane.top(), max(2.0, x2 - x1), lane.height())

    @staticmethod
    def _source_color(src: str) -> QColor:
        palette = NLE["clip_palette"]
        index = sum(ord(char) for char in src) % len(palette) if src else 0
        return QColor(palette[index])

    def _paint_segments(
        self, painter: QPainter, lane: QRectF, track: int = 0
    ) -> None:
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        painter.setPen(QColor(NLE["muted"]))
        painter.drawText(
            QRectF(lane.left() + 2, lane.top() + 1, 22, 12),
            Qt.AlignmentFlag.AlignLeft,
            f"V{track + 1}",
        )
        state = self.track_states.get(track) or {}
        for offset, (field, label) in enumerate(
            (("muted", "M"), ("solo", "S"), ("locked", "L"))
        ):
            active = bool(state.get(field))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(
                QColor(
                    NLE["playhead"] if field == "muted" and active else
                    NLE["solo"] if field == "solo" and active else
                    NLE["text"] if field == "locked" and active else
                    NLE["badge"]
                )
            )
            painter.drawRoundedRect(
                QRectF(lane.left() + 24 + offset * 18, lane.top() + 1, 16, 11),
                2,
                2,
            )
            painter.setPen(QColor(NLE["white"]))
            painter.drawText(
                QRectF(lane.left() + 24 + offset * 18, lane.top() + 1, 16, 11),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )
        for index, segment in enumerate(self.segments):
            if int(segment.get("track") or 0) != track:
                continue
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
                    color = QColor("#22c55e")
                elif key in self._diff_unchanged:
                    color = QColor("#3b82f6")
                else:
                    color = QColor(NLE["muted"])
            else:
                color = (
                    QColor(NLE["solo"])
                    if confidence is not None and confidence < 0.6
                    else self._source_color(str(segment.get("src") or ""))
                )
            painter.setBrush(color)
            painter.setPen(
                QPen(QColor(NLE["selection"]), 2)
                if index == self._selected
                else Qt.PenStyle.NoPen
            )
            painter.drawRoundedRect(segment_rect, 4, 4)
            tag = str(segment.get("tag") or "")
            if tag and segment_rect.width() > 42:
                painter.setPen(QColor(NLE["white"]))
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
                painter.setPen(QColor(NLE["lane"]))
                painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, "FX")
                painter.restore()

        if self._diff_active:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(244, 63, 94, 180))
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
        painter.setPen(QPen(QColor(NLE["solo"]), 1, Qt.PenStyle.DashLine))
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
        painter.setBrush(QColor(NLE["playhead"]))
        for seconds in visible_marks[::step]:
            x = self._time_to_x(seconds, segments)
            painter.drawEllipse(QRectF(x - 2.5, segments.bottom() - 5, 5, 5))

    def _paint_transitions(self, painter: QPainter, lane: QRectF) -> None:
        preview = self._transition_preview
        for index, segment in enumerate(self.segments[:-1]):
            rect = self._segment_rect(segment, lane)
            if rect is None:
                continue
            after = str(segment.get("id") or "")
            spec = self._transition_for(after)
            if spec is None and not (
                preview and preview.get("after") == after
            ):
                continue
            duration = (
                preview["duration"]
                if preview and preview.get("after") == after
                else self._duration_seconds(spec.get("duration") if spec else 0)
            )
            if duration <= 0:
                continue
            right = self.segments[index + 1]
            rect_right = self._segment_rect(right, lane)
            x = rect.right() if rect_right is None else rect_right.left()
            width = max(6.0, min(28.0, duration * 12))
            painter.setPen(QPen(QColor("#38bdf8"), 2))
            painter.setBrush(QColor(56, 189, 248, 70))
            painter.drawRect(QRectF(x - width / 2, lane.top() + 1, width, 8))
            painter.setPen(QColor("#bae6fd"))
            painter.drawText(
                QRectF(x - 32, lane.top() - 12, 64, 12),
                Qt.AlignmentFlag.AlignCenter,
                f"{(spec or {}).get('type', 'fade')} {duration:.1f}s",
            )

    def _paint_snap_line(
        self,
        painter: QPainter,
        ruler: QRectF,
        segments: QRectF,
    ) -> None:
        if self._snap_line is None:
            return
        x = self._time_to_x(self._snap_line, segments)
        painter.setPen(QPen(QColor("#facc15"), 1.5, Qt.PenStyle.DashLine))
        painter.drawLine(x, ruler.top(), x, segments.bottom())
        painter.setPen(QColor("#facc15"))
        painter.drawText(
            QRectF(x + 3, ruler.top(), 40, 12),
            Qt.AlignmentFlag.AlignLeft,
            "吸附",
        )

    def _snap_candidates(self) -> list[tuple[float, int]]:
        values: list[tuple[float, int]] = [(self.position, 0)]
        if self.output_time_enabled:
            spans, _ = self._output_spans()
            for start, end in spans:
                values.append((start, 1))
                values.append((end, 1))
        else:
            for segment in self.segments:
                try:
                    values.append((float(segment["start"]), 1))
                    values.append((float(segment["end"]), 1))
                except (KeyError, TypeError, ValueError):
                    continue
        for shot in self.shots:
            value = seconds_or_none(shot.get("t"))
            if value is not None:
                values.append(
                    (
                        self._source_to_output(value)
                        if self.output_time_enabled
                        else value,
                        2,
                    )
                )
        for mark in self.quality_marks:
            value = seconds_or_none(mark.get("t"))
            if value is not None:
                values.append(
                    (
                        self._source_to_output(value)
                        if self.output_time_enabled
                        else value,
                        3,
                    )
                )
        for item in self.evidence:
            for key in ("start", "end"):
                value = seconds_or_none(item.get(key))
                if value is not None:
                    values.append(
                        (
                            self._source_to_output(value)
                            if self.output_time_enabled
                            else value,
                            4,
                        )
                    )
        for track in self.audio_tracks:
            value = seconds_or_none(track.get("timeline_start"))
            if value is not None:
                values.append((value, 5))
        return values

    def _snap_source(self, value: float) -> float:
        if not self.output_time_enabled:
            return self._snap(value)
        snapped_output = self._snap(self._source_to_output(value))
        return self._output_to_source(snapped_output)

    def _snap(self, value: float, *, exclude: int | None = None) -> float:
        lane = self._lanes()[3]
        if lane.width() <= 0:
            return value
        threshold = 8.0 / lane.width() * self.visible_duration
        best: tuple[float, int, float] | None = None
        for index, (candidate, priority) in enumerate(self._snap_candidates()):
            if exclude is not None and index == exclude:
                continue
            distance = abs(candidate - value)
            if distance > threshold:
                continue
            key = (distance, priority, candidate)
            if best is None or key < best:
                best = key
        return best[2] if best is not None else value

    def _paint_playhead(
        self,
        painter: QPainter,
        ruler: QRectF,
        segments: QRectF,
    ) -> None:
        if not (self.view_start <= self.position <= self.view_end):
            return
        x = self._time_to_x(self.position, segments)
        painter.setPen(QPen(QColor(NLE["playhead"]), 2))
        painter.drawLine(x, ruler.top(), x, self._track_lanes()[-1].bottom())

    def _hit(self, x: float, y: float) -> tuple[int, str] | None:
        for track, lane in enumerate(self._track_lanes()):
            if not (lane.top() - 4 <= y <= lane.bottom() + 4):
                continue
            if lane.top() <= y <= lane.top() + 10:
                for index in range(len(self.segments) - 1):
                    segment = self.segments[index]
                    if int(segment.get("track") or 0) != track:
                        continue
                    rect = self._segment_rect(segment, lane)
                    if rect is not None and abs(x - rect.right()) <= 10:
                        return index, "transition"
            for index in range(len(self.segments) - 1, -1, -1):
                segment = self.segments[index]
                if int(segment.get("track") or 0) != track:
                    continue
                rect = self._segment_rect(segment, lane)
                if rect is None:
                    continue
                if not rect.adjusted(0, -4, 0, 4).contains(x, y):
                    continue
                if x - rect.left() <= EDGE_PX:
                    return index, "left"
                if rect.right() - x <= EDGE_PX:
                    return index, "right"
                return index, "move"
        return None

    def contextMenuEvent(self, event) -> None:
        if self.duration <= 0:
            return
        lane = self._lanes()[3]
        hit = self._hit(event.pos().x(), event.pos().y())
        if hit is None or hit[1] != "transition":
            return
        index, _ = hit
        segment = self.segments[index]
        after = str(segment.get("id") or "")
        spec = self._transition_for(after)
        duration = self._duration_seconds(
            (spec or {}).get("duration")
        ) or 0.5
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        for kind in _TRANSITION_TYPES:
            action = menu.addAction(kind)
            action.setCheckable(True)
            action.setChecked(
                str((spec or {}).get("type") or self.default_transition_type)
                == kind
            )
            action.triggered.connect(
                lambda _checked=False, value=kind: self.transition_changed.emit(
                    after, duration, value
                )
            )
        menu.exec(event.globalPos())

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat("application/x-smartcut-media"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat("application/x-smartcut-media"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        mime = event.mimeData()
        if not mime.hasFormat("application/x-smartcut-media"):
            event.ignore()
            return
        try:
            payload = json.loads(
                bytes(mime.data("application/x-smartcut-media")).decode(
                    "utf-8"
                )
            )
        except (ValueError, UnicodeDecodeError):
            event.ignore()
            return
        lane = self._lanes()[3]
        seconds = self._x_to_time(event.position().x(), lane)
        track = self._track_at_y(event.position().y())
        for asset in payload if isinstance(payload, list) else [payload]:
            if isinstance(asset, dict) and asset.get("src"):
                self.asset_dropped.emit(asset, seconds, track)
        event.acceptProposedAction()

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
        if event.button() == Qt.MouseButton.LeftButton:
            header = self._header_hit(event.position().x(), event.position().y())
            if header is not None:
                track, field = header
                state = dict(self.track_states.get(track) or {})
                state[field] = not bool(state.get(field))
                self.track_states[track] = state
                self.track_state_changed.emit(track, field, bool(state[field]))
                self.update()
                return
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
        if (self.track_states.get(int(segment.get("track") or 0)) or {}).get(
            "locked"
        ):
            self._drag = None
            return
        if zone == "transition":
            after = str(segment.get("id") or "")
            spec = self._transition_for(after)
            self._drag = {
                "zone": "transition",
                "index": index,
                "after": after,
                "type": str(
                    (spec or {}).get("type")
                    or self.default_transition_type
                    or "fade"
                ),
                "duration": self._duration_seconds(
                    (spec or {}).get("duration")
                ),
                "x": event.position().x(),
                "moved": False,
            }
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            return
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
        if self._drag.get("zone") == "transition":
            delta = (
                (event.position().x() - self._drag["x"])
                / lane.width()
                * self.visible_duration
            )
            index = int(self._drag["index"])
            left = self.segments[index]
            right = (
                self.segments[index + 1]
                if index + 1 < len(self.segments)
                else None
            )
            max_left = float(left["end"]) - float(left["start"])
            max_right = (
                float(right["end"]) - float(right["start"])
                if right is not None
                else max_left
            )
            limit = max(0.1, min(max_left, max_right))
            duration = max(
                0.0,
                min(limit, float(self._drag["duration"]) + delta),
            )
            if abs(delta) >= 0.001:
                self._drag["moved"] = True
            self._drag["duration"] = duration
            self._transition_preview = {
                "after": self._drag["after"],
                "duration": duration,
                "type": self._drag["type"],
            }
            self.update()
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
        limit = self._limit_for(self.segments[index] if index < len(self.segments) else None)
        snap_enabled = not (
            event.modifiers() & Qt.KeyboardModifier.AltModifier
        )
        snapped: float | None = None
        if snap_enabled:
            start_candidate = original_start + delta
            end_candidate = original_end + delta
            if zone == "move":
                start_snap = self._snap_source(start_candidate)
                end_snap = self._snap_source(end_candidate)
                start_adj = start_snap - start_candidate
                end_adj = end_snap - end_candidate
                if abs(start_adj) <= abs(end_adj) and start_adj != 0:
                    delta += start_adj
                    snapped = start_snap
                elif end_adj != 0:
                    delta += end_adj
                    snapped = end_snap
            elif zone == "left":
                start_snap = self._snap_source(start_candidate)
                if start_snap != start_candidate:
                    delta = start_snap - original_start
                    snapped = start_snap
            else:
                end_snap = self._snap_source(end_candidate)
                if end_snap != end_candidate:
                    delta = end_snap - original_end
                    snapped = end_snap
        self._snap_line = (
            self._source_to_output(snapped)
            if snapped is not None and self.output_time_enabled
            else snapped
        )
        if zone == "move":
            length = original_end - original_start
            start = max(0.0, min(original_start + delta, limit - length))
            end = start + length
        elif zone == "left":
            start = max(0.0, min(original_start + delta, original_end - MIN_SEGMENT))
            end = original_end
        else:
            start = original_start
            end = min(
                limit,
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
        transition = (
            self._drag
            if self._drag.get("zone") == "transition"
            else None
        )
        self._drag = None
        self._snap_line = None
        self._transition_preview = None
        self.update()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if transition is not None and moved:
            duration = 0.0 if transition["duration"] < 0.05 else transition["duration"]
            self.transition_changed.emit(
                str(transition["after"]),
                duration,
                str(transition["type"]),
            )
        elif moved:
            self.segments_edited.emit([deepcopy(item) for item in self.segments])

    def _emit_seek(self, x: float, lane: QRectF) -> None:
        seconds = max(0.0, min(self.duration, self._x_to_time(x, lane)))
        self.seek_requested.emit(
            self._output_to_source(seconds)
            if self.output_time_enabled
            else seconds
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
