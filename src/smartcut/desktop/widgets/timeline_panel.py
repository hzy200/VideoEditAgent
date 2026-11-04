"""时间线面板：直接编辑片段、撤销/重做、循环选择与 revision diff。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from smartcut.contracts.edit_ir import TimeRange, trim_id_for
from smartcut.contracts.time import RationalTime
from smartcut.desktop.formatting import seconds_or_none
from smartcut.desktop.widgets.thumbnails import ThumbnailCache
from smartcut.desktop.widgets.timeline_widget import MIN_SEGMENT, TimelineWidget


class TimelinePanel(QWidget):
    seek_requested = Signal(float)
    segment_selected = Signal(int, object)
    selection_cleared = Signal()
    modified = Signal(bool)
    undo_requested = Signal()
    redo_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.duration = 0.0
        self.base_ir: dict | None = None
        self.position = 0.0
        self._undo: list[list[dict]] = []
        self._redo: list[list[dict]] = []
        self._last_committed: list[dict] = []
        self._row_ids: list[str | None] = []
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
        layout.addWidget(self.timeline)

        toolbar = QHBoxLayout()
        self.add_button = QPushButton("添加片段")
        self.delete_button = QPushButton("删除")
        self.split_button = QPushButton("在播放头分割")
        self.up_button = QPushButton("上移")
        self.down_button = QPushButton("下移")
        self.undo_button = QPushButton("撤销")
        self.redo_button = QPushButton("重做")
        self.zoom_in_button = QPushButton("放大")
        self.zoom_out_button = QPushButton("缩小")
        self.fit_button = QPushButton("适配")
        for button in (
            self.add_button,
            self.delete_button,
            self.split_button,
            self.up_button,
            self.down_button,
            self.undo_button,
            self.redo_button,
            self.zoom_in_button,
            self.zoom_out_button,
            self.fit_button,
        ):
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.status_label = QLabel("尚无版本")
        self.status_label.setStyleSheet("color:#64748b;")
        layout.addWidget(self.status_label)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["标签", "开始(s)", "结束(s)", "置信度", "依据"]
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

        self.add_button.clicked.connect(self.add_segment)
        self.delete_button.clicked.connect(self.delete_selected)
        self.split_button.clicked.connect(self.split_selected)
        self.up_button.clicked.connect(lambda: self.move_selected(-1))
        self.down_button.clicked.connect(lambda: self.move_selected(1))
        self.undo_button.clicked.connect(self.undo_requested.emit)
        self.redo_button.clicked.connect(self.redo_requested.emit)
        self.zoom_in_button.clicked.connect(self.timeline.zoom_in)
        self.zoom_out_button.clicked.connect(self.timeline.zoom_out)
        self.fit_button.clicked.connect(self.timeline.fit)
        self._update_buttons()

    # ── 数据读写 ──────────────────────────────────────────────────────
    def clear(self) -> None:
        self.base_ir = None
        self.duration = 0.0
        self.position = 0.0
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

    def set_ir(self, ir: dict | None) -> None:
        self.base_ir = ir
        if not ir:
            self.clear()
            return
        facts = ir.get("media_facts") or {}
        self.duration = seconds_or_none(facts.get("duration")) or 0.0
        self.position = 0.0
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

    def segments(self) -> list[dict]:
        segments: list[dict] = []
        for row in range(self.table.rowCount()):
            try:
                start = float(self.table.item(row, 1).text())
                end = float(self.table.item(row, 2).text())
            except (AttributeError, TypeError, ValueError):
                continue
            tag_item = self.table.item(row, 0)
            confidence_item = self.table.item(row, 3)
            rationale_item = self.table.item(row, 4)
            confidence = None
            if confidence_item is not None and confidence_item.text().strip():
                try:
                    confidence = float(confidence_item.text())
                except ValueError:
                    confidence = None
            segments.append(
                {
                    "id": self._row_ids[row] if row < len(self._row_ids) else None,
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

    def _ensure_ids(self, segments: list[dict]) -> None:
        source = str(
            ((self.base_ir or {}).get("media_facts") or {}).get("src") or ""
        )
        used = {segment.get("id") for segment in segments if segment.get("id")}
        for segment in segments:
            if segment.get("id"):
                continue
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
        self.table.blockSignals(True)
        self.table.setRowCount(len(segments))
        for row, segment in enumerate(segments):
            confidence = segment.get("confidence")
            values = [
                str(segment.get("tag") or ""),
                f"{float(segment.get('start') or 0):.3f}",
                f"{float(segment.get('end') or 0):.3f}",
                f"{float(confidence):.2f}" if confidence is not None else "",
                str(segment.get("rationale") or ""),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setToolTip("片段标签，可编辑")
                if column == 4:
                    item.setToolTip(value or "无选段依据")
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
            if start < 0 or end > self.duration + 1e-6:
                return f"第 {index} 段超出素材范围"
            if end - start < MIN_SEGMENT:
                return f"第 {index} 段短于 {MIN_SEGMENT:.1f}s"
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
                "start": start,
                "end": end,
                "tag": tag,
                "confidence": None,
                "rationale": "手动添加",
            },
        )
        self._commit(segments, selected=row)

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
        self.segment_selected.emit(index, segment)

    def _on_widget_cleared(self) -> None:
        self.timeline.select_segment(-1)
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
        segments = self.segments()
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
