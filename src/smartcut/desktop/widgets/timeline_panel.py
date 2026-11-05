"""时间线面板：直接编辑片段、撤销/重做、循环选择与 revision diff。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from smartcut.contracts.edit_ir import TimeRange, trim_id_for
from smartcut.contracts.timeline_ops import TRANSITION_TYPES
from smartcut.contracts.time import RationalTime
from smartcut.desktop.formatting import seconds_or_none
from smartcut.desktop.theme import apply_standard_icon
from smartcut.desktop.widgets.keyframe_dialog import KeyframeDialog
from smartcut.desktop.widgets.thumbnails import ThumbnailCache
from smartcut.desktop.widgets.timeline_widget import MIN_SEGMENT, TimelineWidget


class TimelinePanel(QWidget):
    seek_requested = Signal(float)
    segment_selected = Signal(int, object)
    selection_cleared = Signal()
    modified = Signal(bool)
    transition_changed = Signal(str, float, str)
    track_states_changed = Signal(object)
    preview_requested = Signal()
    undo_requested = Signal()
    redo_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.duration = 0.0
        self.base_ir: dict | None = None
        self.position = 0.0
        self._source_durations: dict[str, float] = {}
        self._source_names: dict[str, str] = {}
        self._track_states: dict[int, dict] = {}
        self._undo: list[list[dict]] = []
        self._redo: list[list[dict]] = []
        self._last_committed: list[dict] = []
        self._row_ids: list[str | None] = []
        self._row_keyframes: list[list[dict]] = []
        self._diff: dict | None = None
        self._dirty = False
        self._thumb_cache = ThumbnailCache(self)
        self._thumb_times: dict[str, float] = {}
        self._thumbnails: list[tuple[float, Any]] = []
        self._thumb_cache.loaded.connect(self._on_thumbnail_ready)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 6)

        self.timeline = TimelineWidget()
        self.timeline.seek_requested.connect(self.seek_requested.emit)
        self.timeline.segment_selected.connect(self._on_widget_selected)
        self.timeline.selection_cleared.connect(self._on_widget_cleared)
        self.timeline.segments_edited.connect(self._on_widget_edited)
        self.timeline.asset_dropped.connect(self._on_asset_dropped)
        self.timeline.transition_changed.connect(
            self.transition_changed.emit
        )
        self.timeline.track_state_changed.connect(
            self._on_track_state_changed
        )
        layout.addWidget(self.timeline)

        # 剪辑工具条：图标分组 + 悬停提示，右侧放转场与主操作。
        toolbar = QHBoxLayout()
        toolbar.setSpacing(3)

        def icon_tool(
            pixmap: QStyle.StandardPixmap, tooltip: str
        ) -> QToolButton:
            button = QToolButton()
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            apply_standard_icon(button, pixmap, 16)
            button.setIconSize(QSize(16, 16))
            button.setFixedSize(28, 26)
            button.setToolTip(tooltip)
            return button

        def text_tool(
            text: str, tooltip: str, width: int = 40
        ) -> QToolButton:
            button = QToolButton()
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setText(text)
            button.setFixedHeight(26)
            button.setMinimumWidth(width)
            button.setToolTip(tooltip)
            return button

        def separator() -> QFrame:
            line = QFrame()
            line.setObjectName("toolbarSeparator")
            line.setFrameShape(QFrame.Shape.VLine)
            line.setFixedSize(1, 20)
            return line

        self.add_button = icon_tool(
            QStyle.StandardPixmap.SP_FileIcon, "在播放头添加片段"
        )
        self.delete_button = icon_tool(
            QStyle.StandardPixmap.SP_TrashIcon, "删除选中片段（Delete）"
        )
        self.split_button = text_tool(
            "分割", "在播放头分割选中片段（Ctrl+B）", 44
        )
        self.up_button = icon_tool(
            QStyle.StandardPixmap.SP_ArrowUp, "片段上移"
        )
        self.down_button = icon_tool(
            QStyle.StandardPixmap.SP_ArrowDown, "片段下移"
        )
        self.undo_button = icon_tool(
            QStyle.StandardPixmap.SP_ArrowBack, "撤销（Ctrl+Z）"
        )
        self.redo_button = icon_tool(
            QStyle.StandardPixmap.SP_ArrowForward,
            "重做（Ctrl+Shift+Z / Ctrl+Y）",
        )
        self.zoom_in_button = text_tool("＋", "放大时间线（滚轮）", 30)
        self.zoom_out_button = text_tool("－", "缩小时间线（滚轮）", 30)
        self.fit_button = text_tool("适配", "适配全部片段", 44)
        self.keyframe_button = text_tool(
            "◆", "记录当前 transform 关键帧", 30
        )
        self.keyframe_edit_button = text_tool(
            "◆…", "打开关键帧曲线编辑器", 36
        )
        self.preview_button = QPushButton("预览成片")
        self.preview_button.setObjectName("primaryButton")
        apply_standard_icon(
            self.preview_button, QStyle.StandardPixmap.SP_MediaPlay, 16
        )
        self.preview_button.setIconSize(QSize(16, 16))
        self.preview_button.setFixedHeight(28)
        self.preview_button.setToolTip("按当前草稿渲染 480p 时间线预览")

        for button in (
            self.add_button,
            self.delete_button,
            self.split_button,
        ):
            toolbar.addWidget(button)
        toolbar.addWidget(separator())
        for button in (self.up_button, self.down_button):
            toolbar.addWidget(button)
        toolbar.addWidget(separator())
        for button in (self.undo_button, self.redo_button):
            toolbar.addWidget(button)
        toolbar.addWidget(separator())
        for button in (
            self.zoom_out_button,
            self.fit_button,
            self.zoom_in_button,
        ):
            toolbar.addWidget(button)
        toolbar.addWidget(separator())
        for button in (self.keyframe_button, self.keyframe_edit_button):
            toolbar.addWidget(button)

        toolbar.addStretch(1)
        transition_label = QLabel("新转场")
        transition_label.setObjectName("mutedText")
        toolbar.addWidget(transition_label)
        self.transition_type = QComboBox()
        for kind in TRANSITION_TYPES:
            self.transition_type.addItem(kind, kind)
        self.transition_type.setToolTip("拖拽创建新转场时使用的类型")
        self.transition_type.setMinimumWidth(110)
        self.transition_type.currentIndexChanged.connect(
            self._on_transition_type_changed
        )
        toolbar.addWidget(self.transition_type)
        toolbar.addSpacing(4)
        toolbar.addWidget(self.preview_button)
        layout.addLayout(toolbar)

        self.status_label = QLabel("尚无版本")
        self.status_label.setObjectName("mutedText")
        layout.addWidget(self.status_label)

        self.table = QTableWidget(0, 12)
        self.table.setHorizontalHeaderLabels(
            [
                "标签",
                "素材",
                "开始(s)",
                "结束(s)",
                "置信度",
                "依据",
                "轨道",
                "X",
                "Y",
                "宽",
                "高",
                "透明度",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.table, 1)

        # clicked(bool) 会把 checked 当成 start 参数；必须显式丢弃。
        self.add_button.clicked.connect(lambda: self.add_segment())
        self.delete_button.clicked.connect(self.delete_selected)
        self.split_button.clicked.connect(self.split_selected)
        self.up_button.clicked.connect(lambda: self.move_selected(-1))
        self.down_button.clicked.connect(lambda: self.move_selected(1))
        self.undo_button.clicked.connect(self.undo_requested.emit)
        self.redo_button.clicked.connect(self.redo_requested.emit)
        self.zoom_in_button.clicked.connect(self.timeline.zoom_in)
        self.zoom_out_button.clicked.connect(self.timeline.zoom_out)
        self.fit_button.clicked.connect(self.timeline.fit)
        self.preview_button.clicked.connect(self.preview_requested.emit)
        self.keyframe_button.clicked.connect(self.add_keyframe)
        self.keyframe_edit_button.clicked.connect(self.edit_keyframes)
        self._update_buttons()

    # ── 数据读写 ──────────────────────────────────────────────────────
    def clear(self) -> None:
        self.base_ir = None
        self.duration = 0.0
        self.position = 0.0
        self._source_durations = {}
        self._source_names = {}
        self._track_states = {}
        self.timeline.set_track_states({})
        self.timeline.set_transitions([])
        self._undo.clear()
        self._redo.clear()
        self._last_committed = []
        self._row_ids = []
        self._diff = None
        self._dirty = False
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.blockSignals(False)
        self.timeline.set_data(0.0, [])
        self.timeline.set_overlays([], [])
        self.timeline.set_evidence([])
        self.timeline.set_thumbnails([])
        self.timeline.set_waveform([])
        self.timeline.set_audio_tracks([])
        self.timeline.clear_diff()
        self._thumbnails.clear()
        self._thumb_times.clear()
        self.status_label.setText("尚无版本")
        self._update_buttons()

    def set_perception(self, data: dict | None) -> None:
        shots = ((data or {}).get("shots") or {}).get("shots") or []
        marks = ((data or {}).get("quality") or {}).get("marks") or []
        self.timeline.set_overlays(shots, marks)

    def set_evidence(self, payload: dict | None) -> None:
        if not payload or not payload.get("available"):
            self.timeline.set_evidence([])
            return
        evidence = payload.get("evidence") or {}
        items: list[dict] = []
        for tile in (evidence.get("tile_map") or {}).get("tiles") or []:
            try:
                seconds = float(tile["t"]["value"]) / float(tile["t"]["rate"])
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
            items.append(
                {
                    "start": max(0.0, seconds - 0.2),
                    "end": seconds + 0.2,
                    "kind": "tile",
                    "label": str(tile.get("label") or ""),
                }
            )
        for gap in payload.get("silence") or []:
            items.append(
                {
                    "start": float(gap.get("start") or 0.0),
                    "end": float(gap.get("end") or 0.0),
                    "kind": "silence",
                    "label": "静音",
                }
            )
        for window in (payload.get("selection") or {}).get("windows") or []:
            try:
                start = (
                    float(window["start"]["value"]) / float(window["start"]["rate"])
                )
                end = float(window["end"]["value"]) / float(window["end"]["rate"])
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
            items.append(
                {
                    "start": start,
                    "end": end,
                    "kind": "candidate",
                    "label": "候选窗口",
                }
            )
        self.timeline.set_evidence(items)

    def set_frame_manifest(self, frames: list[dict], workspace: Path) -> None:
        self._thumb_times.clear()
        self._thumbnails.clear()
        self.timeline.set_thumbnails([])
        if not frames:
            return
        sample = frames[:: max(1, len(frames) // 40)][:40]
        for frame in sample:
            seconds = seconds_or_none(frame.get("t"))
            relative = frame.get("path")
            if seconds is None or not relative:
                continue
            path = workspace / str(relative)
            key = ThumbnailCache.key_for(str(path), (64, 36))
            self._thumb_times[key] = seconds
            pixmap = self._thumb_cache.get(str(path), (64, 36))
            if pixmap is not None:
                self._thumbnails.append((seconds, pixmap))
        self._thumbnails.sort(key=lambda item: item[0])
        self.timeline.set_thumbnails(self._thumbnails)

    def set_waveform(self, payload: dict | None) -> None:
        peaks = (payload or {}).get("peaks") or []
        self.timeline.set_waveform(peaks)

    def set_audio_tracks(self, tracks: list[dict] | None) -> None:
        self.timeline.set_audio_tracks(tracks)

    def set_audio_waveforms(self, payload: dict | None) -> None:
        self.timeline.set_audio_waveforms(payload)

    def _on_thumbnail_ready(self, key: str, pixmap) -> None:
        seconds = self._thumb_times.get(key)
        if seconds is None:
            return
        self._thumbnails.append((seconds, pixmap))
        self._thumbnails.sort(key=lambda item: item[0])
        self.timeline.set_thumbnails(self._thumbnails)

    def set_transitions(self, transitions: list[dict] | None) -> None:
        self.timeline.set_transitions(transitions)

    def set_pips(self, pips: list[dict] | None) -> None:
        self.timeline.set_pips(pips)

    def set_track_states(self, states: dict | None) -> None:
        self._track_states = {
            int(track): dict(value)
            for track, value in (states or {}).items()
        }
        self.timeline.set_track_states(self._track_states)

    def track_states(self) -> dict:
        return {
            int(track): dict(value)
            for track, value in self._track_states.items()
        }

    def _on_track_state_changed(
        self, track: int, field: str, value: bool
    ) -> None:
        state = dict(self._track_states.get(int(track)) or {})
        state[field] = bool(value)
        self._track_states[int(track)] = state
        self.track_states_changed.emit(self.track_states())

    def _on_transition_type_changed(self) -> None:
        self.timeline.default_transition_type = str(
            self.transition_type.currentData() or "fade"
        )

    def set_source_assets(self, assets: list[dict]) -> None:
        self._source_durations = {}
        self._source_names = {}
        for asset in assets or []:
            src = str(asset.get("src") or "")
            if not src:
                continue
            facts = asset.get("facts") or {}
            duration = seconds_or_none(facts.get("duration"))
            if duration is not None:
                self._source_durations[src] = duration
            self._source_names[src] = str(
                asset.get("name") or Path(src).stem
            )
        self.timeline.set_source_durations(self._source_durations)

    def set_ir(self, ir: dict | None) -> None:
        self.base_ir = ir
        if not ir:
            self.clear()
            return
        self.set_track_states(ir.get("track_states") or {})
        facts = ir.get("media_facts") or {}
        primary_src = str(facts.get("src") or "")
        primary_duration = seconds_or_none(facts.get("duration")) or 0.0
        self.duration = primary_duration
        self.position = 0.0
        self._source_durations[primary_src] = primary_duration
        self._source_names.setdefault(primary_src, Path(primary_src).stem)
        for src, source in (ir.get("source_facts") or {}).items():
            duration = seconds_or_none((source or {}).get("duration"))
            if duration is not None:
                self._source_durations[str(src)] = duration
                self.duration = max(self.duration, duration)
            self._source_names.setdefault(str(src), Path(str(src)).stem)
        self.timeline.set_source_durations(self._source_durations)
        segments: list[dict] = []
        for op in ir.get("sequence") or []:
            time_range = op.get("range") or {}
            start = seconds_or_none(time_range.get("start"))
            end = seconds_or_none(time_range.get("end"))
            if start is None or end is None:
                continue
            segments.append(
                {
                    "id": op.get("id"),
                    "src": str(op.get("src") or primary_src),
                    "track": int(op.get("track") or 0),
                    "timeline_start": seconds_or_none(
                        op.get("timeline_start")
                    ),
                    "transform": op.get("transform"),
                    "keyframes": op.get("keyframes") or [],
                    "speed": float(op.get("speed") or 1.0),
                    "start": start,
                    "end": end,
                    "tag": op.get("tag") or "",
                    "confidence": op.get("confidence"),
                    "rationale": op.get("rationale"),
                }
            )
        self._undo.clear()
        self._redo.clear()
        self._diff = None
        self._dirty = False
        self.timeline.clear_diff()
        self._populate(segments)
        self.status_label.setText(
            f"revision {ir.get('revision', '-')} · {len(segments)} 段 · {self.duration:.2f}s"
        )
        self._update_buttons()

    def segments(self, *, strict: bool = False) -> list[dict]:
        segments: list[dict] = []
        for row in range(self.table.rowCount()):
            try:
                start = float(self.table.item(row, 2).text())
                end = float(self.table.item(row, 3).text())
            except (AttributeError, TypeError, ValueError) as exc:
                if strict:
                    raise ValueError(
                        f"第 {row + 1} 行的起止时间必须是数字"
                    ) from exc
                continue
            tag_item = self.table.item(row, 0)
            source_item = self.table.item(row, 1)
            confidence_item = self.table.item(row, 4)
            rationale_item = self.table.item(row, 5)
            track_item = self.table.item(row, 6)
            transform_items = [self.table.item(row, column) for column in range(7, 12)]
            confidence = None
            if confidence_item is not None and confidence_item.text().strip():
                try:
                    confidence = float(confidence_item.text())
                except ValueError:
                    confidence = None
            segments.append(
                {
                    "id": self._row_ids[row] if row < len(self._row_ids) else None,
                    "src": (
                        source_item.data(Qt.ItemDataRole.UserRole)
                        if source_item is not None
                        else None
                    )
                    or self._default_src(),
                    "track": (
                        int(track_item.text())
                        if track_item is not None
                        and track_item.text().strip().isdigit()
                        else 0
                    ),
                    "transform": (
                        {
                            "x": float(transform_items[0].text()),
                            "y": float(transform_items[1].text()),
                            "width": float(transform_items[2].text()),
                            "height": float(transform_items[3].text()),
                            "opacity": float(transform_items[4].text()),
                        }
                        if all(item is not None for item in transform_items)
                        else None
                    ),
                    "keyframes": (
                        deepcopy(self._row_keyframes[row])
                        if row < len(self._row_keyframes)
                        else []
                    ),
                    "start": start,
                    "end": end,
                    "tag": tag_item.text().strip() if tag_item else "",
                    "confidence": confidence,
                    "rationale": (
                        rationale_item.text().strip() if rationale_item else ""
                    ),
                }
            )
        return segments

    def _default_src(self) -> str:
        return str(
            ((self.base_ir or {}).get("media_facts") or {}).get("src") or ""
        )

    def _ensure_ids(self, segments: list[dict]) -> None:
        used = {segment.get("id") for segment in segments if segment.get("id")}
        for segment in segments:
            if segment.get("id"):
                continue
            source = str(segment.get("src") or self._default_src())
            base = trim_id_for(
                source,
                TimeRange(
                    start=RationalTime.from_seconds(
                        float(segment.get("start") or 0), rate=90_000
                    ),
                    end=RationalTime.from_seconds(
                        float(segment.get("end") or 0), rate=90_000
                    ),
                ),
            )
            candidate = base
            suffix = 1
            while candidate in used:
                candidate = f"{base}_{suffix}"
                suffix += 1
            used.add(candidate)
            segment["id"] = candidate

    def _populate(self, segments: list[dict], selected: int | None = None) -> None:
        self._ensure_ids(segments)
        self._last_committed = deepcopy(segments)
        self._row_ids = [segment.get("id") for segment in segments]
        self._row_keyframes = [
            deepcopy(segment.get("keyframes") or []) for segment in segments
        ]
        self.table.blockSignals(True)
        self.table.setRowCount(len(segments))
        for row, segment in enumerate(segments):
            confidence = segment.get("confidence")
            src = str(segment.get("src") or self._default_src())
            values = [
                str(segment.get("tag") or ""),
                self._source_names.get(src, Path(src).stem if src else "-"),
                f"{float(segment.get('start') or 0):.3f}",
                f"{float(segment.get('end') or 0):.3f}",
                f"{float(confidence):.2f}" if confidence is not None else "",
                str(segment.get("rationale") or ""),
                str(int(segment.get("track") or 0)),
                *[
                    f"{float(value):.3f}"
                    for value in (
                        (segment.get("transform") or {}).get("x", 0.0),
                        (segment.get("transform") or {}).get("y", 0.0),
                        (segment.get("transform") or {}).get("width", 1.0),
                        (segment.get("transform") or {}).get("height", 1.0),
                        (segment.get("transform") or {}).get("opacity", 1.0),
                    )
                ],
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setToolTip("片段标签，可编辑")
                if column == 1:
                    item.setData(Qt.ItemDataRole.UserRole, src)
                    item.setToolTip(src)
                    item.setFlags(
                        item.flags() & ~Qt.ItemFlag.ItemIsEditable
                    )
                if column == 5:
                    item.setToolTip(value or "无选段依据")
                    item.setFlags(
                        item.flags() & ~Qt.ItemFlag.ItemIsEditable
                    )
                if column == 6:
                    item.setToolTip("视频轨编号（0=V1）")
                    item.setFlags(
                        item.flags() & ~Qt.ItemFlag.ItemIsEditable
                    )
                self.table.setItem(row, column, item)
        if selected is not None and 0 <= selected < len(segments):
            self.table.setCurrentCell(selected, 0)
        self.table.blockSignals(False)
        self.timeline.set_data(self.duration, segments)
        self._update_buttons()

    def _validate(self, segments: list[dict]) -> str | None:
        if self.duration <= 0:
            return "时间线时长为 0"
        for index, segment in enumerate(segments, start=1):
            start = float(segment.get("start") or 0)
            end = float(segment.get("end") or 0)
            src = str(segment.get("src") or self._default_src())
            limit = self._source_durations.get(src, self.duration)
            if start < 0 or end > limit + 1e-6:
                return (
                    f"第 {index} 段超出素材范围"
                    f"（{self._source_names.get(src, src)} {limit:.2f}s）"
                )
            if end - start < MIN_SEGMENT:
                return f"第 {index} 段短于 {MIN_SEGMENT:.1f}s"
            transform = segment.get("transform")
            if transform:
                try:
                    x = float(transform.get("x", 0.0))
                    y = float(transform.get("y", 0.0))
                    width = float(transform.get("width", 1.0))
                    height = float(transform.get("height", 1.0))
                    opacity = float(transform.get("opacity", 1.0))
                except (TypeError, ValueError):
                    return f"第 {index} 段 transform 必须是数字"
                if not (0 <= x <= 1 and 0 <= y <= 1):
                    return f"第 {index} 段 transform 原点必须在 0..1"
                if width <= 0 or height <= 0 or x + width > 1.0001 or y + height > 1.0001:
                    return f"第 {index} 段 transform 超出画布"
                if not (0 <= opacity <= 1):
                    return f"第 {index} 段透明度必须在 0..1"
        return None

    def _commit(
        self,
        segments: list[dict],
        *,
        push: bool = True,
        selected: int | None = None,
    ) -> bool:
        error = self._validate(segments)
        if error:
            self.status_label.setText(error)
            self._populate(self._last_committed, selected=self.table.currentRow())
            return False
        changed = push and segments != self._last_committed
        if changed:
            self._undo.append(deepcopy(self._last_committed))
            if len(self._undo) > 100:
                self._undo.pop(0)
            self._redo.clear()
            self._dirty = True
        self._diff = None
        self.timeline.clear_diff()
        self._populate(segments, selected=selected)
        self.status_label.setText(
            f"{len(segments)} 段 · {sum(max(0.0, s['end'] - s['start']) for s in segments):.2f}s"
        )
        if changed:
            self.modified.emit(True)
        return True

    # ── 编辑操作 ──────────────────────────────────────────────────────
    def add_segment(
        self,
        start: float | None = None,
        end: float | None = None,
        tag: str = "manual",
    ) -> None:
        if self.duration <= 0:
            return
        if start is None:
            start = self.position if self.position < self.duration - 0.2 else 0.0
            end = min(self.duration, start + 3.0)
            if end - start < MIN_SEGMENT:
                start = max(0.0, self.duration - 1.0)
                end = self.duration
        start = max(0.0, min(float(start), self.duration - MIN_SEGMENT))
        end = max(start + MIN_SEGMENT, min(float(end), self.duration))
        segments = self.segments()
        row = max(0, self.table.currentRow() + 1)
        segments.insert(
            row,
            {
                "id": None,
                "src": self._default_src(),
                "track": 0,
                "start": start,
                "end": end,
                "tag": tag,
                "confidence": None,
                "rationale": "手动添加",
            },
        )
        self._commit(segments, selected=row)

    def add_source_segment(
        self, asset: dict, seconds: float, track: int = 0
    ) -> None:
        src = str(asset.get("src") or "")
        facts = asset.get("facts") or {}
        source_duration = seconds_or_none(facts.get("duration"))
        if not src or not source_duration:
            return
        self._source_durations[src] = source_duration
        self._source_names[src] = str(
            asset.get("name") or Path(src).stem
        )
        self.timeline.set_source_durations(self._source_durations)
        start = max(0.0, min(float(seconds), max(0.0, source_duration - MIN_SEGMENT)))
        end = min(source_duration, start + 5.0)
        if end - start < MIN_SEGMENT:
            start = max(0.0, source_duration - 5.0)
            end = source_duration
        segments = self.segments()
        row = len(segments)
        segments.append(
            {
                "id": None,
                "src": src,
                "track": max(0, int(track)),
                "timeline_start": start,
                "transform": (
                    {
                        "x": 0.65,
                        "y": 0.05,
                        "width": 0.3,
                        "height": 0.3,
                        "opacity": 1.0,
                    }
                    if int(track) > 0
                    else None
                ),
                "start": start,
                "end": end,
                "tag": str(asset.get("name") or Path(src).stem),
                "confidence": None,
                "rationale": "从素材 bin 拖入",
            }
        )
        self.duration = max(self.duration, end)
        self._commit(segments, selected=row)

    def add_keyframe(self) -> None:
        row = self.table.currentRow()
        segments = self.segments()
        if row < 0 or row >= len(segments):
            self.status_label.setText("请先选择叠加轨片段")
            return
        segment = segments[row]
        if self._track_locked(int(segment.get("track") or 0)):
            self.status_label.setText("轨道已锁定，禁止编辑")
            return
        transform = segment.get("transform")
        if int(segment.get("track") or 0) <= 0 or not transform:
            self.status_label.setText("关键帧只用于 V2+ 叠加轨")
            return
        start = float(segment.get("start") or 0.0)
        end = float(segment.get("end") or start)
        local = max(0.0, min(end - start, self.position - start))
        keyframes = list(segment.get("keyframes") or [])
        keyframes = [
            item
            for item in keyframes
            if abs(
                RationalTime.model_validate(item["t"]).to_float() - local
            )
            > 1e-4
        ]
        keyframes.append(
            {
                "t": RationalTime.from_seconds(
                    f"{local:.6f}", rate=90_000
                ).model_dump(),
                "x": float(transform.get("x", 0.0)),
                "y": float(transform.get("y", 0.0)),
                "width": float(transform.get("width", 1.0)),
                "height": float(transform.get("height", 1.0)),
                "opacity": float(transform.get("opacity", 1.0)),
            }
        )
        keyframes.sort(
            key=lambda item: RationalTime.model_validate(item["t"]).to_float()
        )
        segment["keyframes"] = keyframes
        self._commit(segments, selected=row)
        self.status_label.setText(
            f"已添加关键帧 @ {local:.2f}s（共 {len(keyframes)} 个）"
        )

    def edit_keyframes(self) -> None:
        row = self.table.currentRow()
        segments = self.segments()
        if row < 0 or row >= len(segments):
            self.status_label.setText("请先选择叠加轨片段")
            return
        segment = segments[row]
        if self._track_locked(int(segment.get("track") or 0)):
            self.status_label.setText("轨道已锁定，禁止编辑")
            return
        if int(segment.get("track") or 0) <= 0:
            self.status_label.setText("关键帧只用于 V2+ 叠加轨")
            return
        dialog = KeyframeDialog(segment.get("keyframes") or [], parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            segment["keyframes"] = dialog.keyframes()
            self._commit(segments, selected=row)
            self.status_label.setText(
                f"关键帧已更新（共 {len(segment['keyframes'])} 个）"
            )

    def add_pip_clip(
        self, asset: dict, timeline_start: float
    ) -> None:
        src = str(asset.get("src") or "")
        facts = asset.get("facts") or {}
        duration = seconds_or_none(facts.get("duration"))
        if not src or not duration:
            return
        self._source_durations[src] = duration
        self._source_names[src] = str(
            asset.get("name") or Path(src).stem
        )
        self.timeline.set_source_durations(self._source_durations)
        start = max(0.0, float(timeline_start))
        clip_duration = min(5.0, duration)
        segments = self.segments()
        row = len(segments)
        segments.append(
            {
                "id": None,
                "src": src,
                "track": 1,
                "timeline_start": start,
                "transform": {
                    "x": 0.65,
                    "y": 0.05,
                    "width": 0.3,
                    "height": 0.3,
                    "opacity": 1.0,
                },
                "start": 0.0,
                "end": clip_duration,
                "tag": str(asset.get("name") or Path(src).stem),
                "confidence": None,
                "rationale": "画中画",
            }
        )
        self.duration = max(self.duration, start + clip_duration)
        self._commit(segments, selected=row)

    def update_segment_transform(
        self, segment_id: str, transform: dict
    ) -> bool:
        segments = self.segments()
        for segment in segments:
            if str(segment.get("id")) == str(segment_id):
                segment["transform"] = dict(transform)
                self._commit(segments, selected=self.table.currentRow())
                return True
        return False

    def _on_asset_dropped(
        self, asset: dict, seconds: float, track: int = 0
    ) -> None:
        if self._track_locked(int(track)):
            self.status_label.setText("轨道已锁定，禁止拖入")
            return
        self.add_source_segment(asset, seconds, track)

    def add_candidates(self, candidates: list[dict]) -> None:
        if not candidates or self.duration <= 0:
            return
        segments = self.segments()
        row = len(segments)
        for candidate in candidates:
            start = seconds_or_none(candidate.get("start"))
            end = seconds_or_none(candidate.get("end"))
            if start is None or end is None:
                start = candidate.get("start")
                end = candidate.get("end")
            try:
                start = float(start)
                end = float(end)
            except (TypeError, ValueError):
                continue
            start = max(0.0, min(start, self.duration - MIN_SEGMENT))
            end = max(start + MIN_SEGMENT, min(end, self.duration))
            tags = candidate.get("tags") or []
            tag = str(tags[0]) if tags else "候选"
            text = str(candidate.get("text") or "").strip()
            rationale = (f"候选：{text[:50]}" if text else "候选片段").strip()
            segments.append(
                {
                    "id": None,
                    "src": self._default_src(),
                    "track": 0,
                    "start": start,
                    "end": end,
                    "tag": tag,
                    "confidence": candidate.get("score"),
                    "rationale": rationale,
                }
            )
        if len(segments) == row:
            return
        self._commit(segments, selected=row)

    def _track_locked(self, track: int) -> bool:
        return bool(
            (self._track_states.get(int(track)) or {}).get("locked")
        )

    def _rows_unlocked(self, rows: list[int], segments: list[dict]) -> bool:
        for row in rows:
            if 0 <= row < len(segments) and self._track_locked(
                int(segments[row].get("track") or 0)
            ):
                self.status_label.setText(
                    f"轨道 V{int(segments[row].get('track') or 0) + 1} 已锁定"
                )
                return False
        return True

    def _selected_rows(self) -> list[int]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        return rows

    def delete_selected(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        segments = self.segments()
        if not self._rows_unlocked(rows, segments):
            return
        for row in sorted(rows, reverse=True):
            if 0 <= row < len(segments):
                segments.pop(row)
        target = max(0, min(rows[0], len(segments) - 1))
        self._commit(segments, selected=target if segments else None)

    def split_selected(self) -> None:
        rows = self._selected_rows()
        if not rows:
            rows = [
                index
                for index, segment in enumerate(self.segments())
                if segment["start"] < self.position < segment["end"]
            ]
        if not rows:
            return
        segments = self.segments()
        if not self._rows_unlocked(rows, segments):
            return
        output: list[dict] = []
        split_row: int | None = None
        for index, segment in enumerate(segments):
            if (
                index in rows
                and segment["start"] + MIN_SEGMENT < self.position
                and self.position < segment["end"] - MIN_SEGMENT
            ):
                output.append({**segment, "end": self.position})
                if split_row is None:
                    split_row = len(output) - 1
                output.append(
                    {**segment, "start": self.position, "id": None}
                )
            else:
                output.append(segment)
        if split_row is None:
            self.status_label.setText("播放头不在所选片段内部")
            return
        self._commit(output, selected=split_row)

    def move_selected(self, delta: int) -> None:
        row = self.table.currentRow()
        target = row + delta
        segments = self.segments()
        if row < 0 or target < 0 or target >= len(segments):
            return
        if not self._rows_unlocked([row], segments):
            return
        segments[row], segments[target] = segments[target], segments[row]
        self._commit(segments, selected=target)

    def undo(self) -> None:
        if not self._undo:
            return
        self._redo.append(deepcopy(self._last_committed))
        segments = self._undo.pop()
        self._dirty = True
        self.modified.emit(True)
        self._diff = None
        self.timeline.clear_diff()
        self._populate(segments, selected=min(self.table.currentRow(), len(segments) - 1))
        self.status_label.setText("已撤销")

    def redo(self) -> None:
        if not self._redo:
            return
        self._undo.append(deepcopy(self._last_committed))
        segments = self._redo.pop()
        self._dirty = True
        self.modified.emit(True)
        self._diff = None
        self.timeline.clear_diff()
        self._populate(segments, selected=min(self.table.currentRow(), len(segments) - 1))
        self.status_label.setText("已重做")

    def restore_segments(self, segments: list[dict]) -> None:
        """由统一编辑会话恢复快照；不写入本面板自己的撤销栈。"""
        self._undo.clear()
        self._redo.clear()
        self._diff = None
        self._dirty = False
        self.timeline.clear_diff()
        self._populate(deepcopy(segments))
        self.status_label.setText(f"{len(segments)} 段")

    # ── 选择 / 播放头 / diff ─────────────────────────────────────────
    def set_position(self, seconds: float) -> None:
        self.position = max(0.0, float(seconds))
        self.timeline.set_position(self.position)

    def set_effect_segments(self, segment_ids: set[str] | list[str] | None) -> None:
        self.timeline.set_effect_segments(segment_ids)

    def set_diff(self, diff: dict | None) -> None:
        self._diff = diff
        if not diff:
            self.timeline.clear_diff()
            return
        self.timeline.set_diff(
            diff.get("added") or [],
            diff.get("removed") or [],
            diff.get("unchanged") or [],
        )
        self.status_label.setText(
            f"diff：+{diff.get('added_count', 0)} "
            f"-{diff.get('removed_count', 0)} "
            f"={diff.get('unchanged_count', 0)}"
        )

    def _select_row(self, row: int) -> None:
        self.table.blockSignals(True)
        self.table.setCurrentCell(row, 0)
        self.table.blockSignals(False)
        self.timeline.select_segment(row)

    def _on_widget_selected(self, index: int, segment: dict) -> None:
        self._select_row(index)
        self._update_buttons()
        self.segment_selected.emit(index, segment)

    def _on_widget_cleared(self) -> None:
        self.timeline.select_segment(-1)
        self._update_buttons()
        self.selection_cleared.emit()

    def _on_widget_edited(self, segments: list[dict]) -> None:
        self._commit(segments, selected=self.table.currentRow())

    def _on_selection_changed(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            self.timeline.select_segment(-1)
            self.selection_cleared.emit()
            return
        self.timeline.select_segment(row)
        segment = self.segments()[row] if row < len(self.segments()) else None
        if segment is not None:
            self.segment_selected.emit(row, segment)

    def _on_item_changed(self, _item: QTableWidgetItem) -> None:
        row = self.table.currentRow()
        if row >= 0:
            track_item = self.table.item(row, 6)
            if (
                track_item is not None
                and track_item.text().strip().isdigit()
                and self._track_locked(int(track_item.text()))
            ):
                self.status_label.setText("轨道已锁定，禁止编辑")
                self._populate(self._last_committed, selected=row)
                return
        try:
            segments = self.segments(strict=True)
        except ValueError as exc:
            self.status_label.setText(str(exc))
            self._populate(
                self._last_committed, selected=self.table.currentRow()
            )
            return
        error = self._validate(segments)
        if error:
            self.status_label.setText(error)
            self._populate(self._last_committed, selected=self.table.currentRow())
            return
        if segments != self._last_committed:
            self._undo.append(deepcopy(self._last_committed))
            if len(self._undo) > 100:
                self._undo.pop(0)
            self._redo.clear()
            self._dirty = True
            self.modified.emit(True)
            self._last_committed = deepcopy(segments)
            self._diff = None
            self.timeline.clear_diff()
            self.timeline.set_data(self.duration, segments)
            self._update_buttons()
            self.status_label.setText("已修改时间线")

    def _update_buttons(self) -> None:
        has_segments = self.table.rowCount() > 0
        has_selection = bool(self._selected_rows()) if has_segments else False
        self.delete_button.setEnabled(has_selection)
        self.split_button.setEnabled(has_selection)
        self.up_button.setEnabled(has_selection)
        self.down_button.setEnabled(has_selection)
        self.undo_button.setEnabled(bool(self._undo))
        self.redo_button.setEnabled(bool(self._redo))
        self.add_button.setEnabled(self.duration > 0)
