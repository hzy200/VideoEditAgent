"""音频面板：全局主输出 + 多轨放置、增益、静音/独奏与淡入淡出。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from smartcut.desktop.formatting import seconds_or_none


class AudioPanel(QWidget):
    audio_changed = Signal(object)
    tracks_changed = Signal(object)
    import_requested = Signal()
    separate_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False
        self._assets: dict[str, dict] = {}
        self._main_duration = 0.0
        self._playhead = 0.0
        self._legacy_main = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        hint = QLabel(
            "全局音量/音调作用于最终混音；音轨增益、静音、独奏与淡入淡出"
            "在渲染时由单次 filter_complex 应用。试听音量请用预览播放器。"
        )
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        master = QGroupBox("主输出")
        form = QFormLayout(master)
        volume_row = QHBoxLayout()
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(-300, 120)
        self.volume_slider.setSingleStep(5)
        self.volume_slider.setPageStep(30)
        self.volume_slider.setValue(0)
        self.volume_label = QLabel("0.0 dB")
        self.volume_label.setObjectName("accentText")
        self.volume_label.setMinimumWidth(72)
        volume_row.addWidget(self.volume_slider, 1)
        volume_row.addWidget(self.volume_label)
        form.addRow("音量", volume_row)

        pitch_row = QHBoxLayout()
        self.pitch_slider = QSlider(Qt.Orientation.Horizontal)
        self.pitch_slider.setRange(-12, 12)
        self.pitch_slider.setSingleStep(1)
        self.pitch_slider.setValue(0)
        self.pitch_label = QLabel("0 半音")
        self.pitch_label.setObjectName("accentText")
        self.pitch_label.setMinimumWidth(72)
        pitch_row.addWidget(self.pitch_slider, 1)
        pitch_row.addWidget(self.pitch_label)
        form.addRow("音调", pitch_row)

        self.reset_button = QPushButton("恢复默认")
        form.addRow("", self.reset_button)
        layout.addWidget(master)

        tracks_group = QGroupBox("音轨")
        tracks_layout = QVBoxLayout(tracks_group)
        asset_row = QHBoxLayout()
        self.asset_combo = QComboBox()
        self.import_button = QPushButton("导入音频")
        self.add_button = QPushButton("添加音轨")
        self.remove_button = QPushButton("删除选中")
        asset_row.addWidget(self.asset_combo, 1)
        asset_row.addWidget(self.import_button)
        asset_row.addWidget(self.add_button)
        asset_row.addWidget(self.remove_button)
        tracks_layout.addLayout(asset_row)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            [
                "名称",
                "素材",
                "起点(s)",
                "增益(dB)",
                "静音",
                "独奏",
                "循环",
                "淡入(s)",
                "淡出(s)",
            ]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in range(2, 9):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        tracks_layout.addWidget(self.table, 1)

        footer = QHBoxLayout()
        self.separate_button = QPushButton("分离人声/伴奏")
        self.separate_button.setToolTip(
            "使用外部 Demucs 离线分离，生成可加入音轨的素材"
        )
        self.status_label = QLabel("尚无音轨")
        self.status_label.setObjectName("mutedText")
        footer.addWidget(self.separate_button)
        footer.addStretch(1)
        footer.addWidget(self.status_label)
        tracks_layout.addLayout(footer)
        layout.addWidget(tracks_group, 1)

        self.volume_slider.valueChanged.connect(self._on_changed)
        self.pitch_slider.valueChanged.connect(self._on_changed)
        self.reset_button.clicked.connect(self.reset)
        self.import_button.clicked.connect(self.import_requested.emit)
        self.separate_button.clicked.connect(self.separate_requested.emit)
        self.add_button.clicked.connect(self.add_selected_asset)
        self.remove_button.clicked.connect(self.remove_selected)
        self.table.itemChanged.connect(self._on_item_changed)
        self._refresh_labels()
        self._update_status()

    def set_assets(self, assets: list[dict] | None) -> None:
        current = self.asset_combo.currentData()
        self._assets = {str(item.get("id")): dict(item) for item in assets or []}
        self.asset_combo.blockSignals(True)
        self.asset_combo.clear()
        for asset_id, asset in self._assets.items():
            duration = _duration(asset.get("facts") or {})
            label = f"{asset.get('name') or asset_id}"
            if duration > 0:
                label += f" · {duration:.1f}s"
            self.asset_combo.addItem(label, asset_id)
        self.asset_combo.blockSignals(False)
        if current is not None:
            index = self.asset_combo.findData(current)
            if index >= 0:
                self.asset_combo.setCurrentIndex(index)

    def set_context(self, *, main_duration: float) -> None:
        self._main_duration = max(0.0, float(main_duration or 0.0))

    def set_playhead(self, seconds: float) -> None:
        self._playhead = max(0.0, float(seconds or 0.0))

    def set_tracks(self, tracks: list[dict] | None, *, main_duration: float | None = None) -> None:
        if main_duration is not None:
            self.set_context(main_duration=main_duration)
        rows = list(tracks or [])
        self._legacy_main = tracks is None and self._main_duration > 0
        if self._legacy_main:
            rows = [self._main_track()]
        self._loading = True
        try:
            self.table.setRowCount(len(rows))
            for row, track in enumerate(rows):
                self._populate_row(row, track)
        finally:
            self._loading = False
        self._update_status()

    def tracks(self) -> list[dict[str, Any]]:
        tracks, errors = self._collect()
        return [] if errors else tracks

    def errors(self) -> list[str]:
        return self._collect()[1]

    def add_selected_asset(self) -> None:
        asset_id = str(self.asset_combo.currentData() or "")
        if asset_id not in self._assets:
            return
        asset = self._assets[asset_id]
        duration = _duration(asset.get("facts") or {})
        if duration <= 0:
            return
        display_name = str(asset.get("name") or asset_id)
        lowered = display_name.lower()
        if "人声" in display_name or "vocal" in lowered:
            role = "stem_vocals"
        elif "伴奏" in display_name or "instrument" in lowered:
            role = "stem_instrumental"
        else:
            role = "music"
        track = {
            "id": self._next_id(),
            "name": display_name,
            "role": role,
            "asset_id": asset_id,
            "source_start": 0.0,
            "source_end": duration,
            "timeline_start": max(0.0, self._playhead),
            "gain_db": 0.0,
            "muted": False,
            "solo": False,
            "loop": False,
            "fade_in": 0.0,
            "fade_out": 0.0,
        }
        if self._legacy_main and self.table.rowCount() == 0 and self._main_duration > 0:
            self.table.blockSignals(True)
            try:
                self.table.insertRow(0)
                self._populate_row(0, self._main_track())
            finally:
                self.table.blockSignals(False)
            self._legacy_main = False
        row = self.table.rowCount()
        self._loading = True
        try:
            self.table.insertRow(row)
            self._populate_row(row, track)
            self.table.setCurrentCell(row, 0)
        finally:
            self._loading = False
        self._emit_tracks()

    def remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        self._legacy_main = False
        self._loading = True
        try:
            for row in rows:
                self.table.removeRow(row)
        finally:
            self._loading = False
        self._emit_tracks()

    def set_audio(self, audio: dict | None, *, notify: bool = False) -> None:
        payload = audio or {}
        volume = float(payload.get("volume_db") or 0.0)
        pitch = float(payload.get("pitch_semitones") or 0.0)
        self._loading = True
        try:
            self.volume_slider.setValue(
                int(round(max(-30.0, min(12.0, volume)) * 10))
            )
            self.pitch_slider.setValue(int(round(max(-12.0, min(12.0, pitch)))))
            self._refresh_labels()
        finally:
            self._loading = False
        if notify:
            self._emit_master()

    def audio(self) -> dict[str, Any]:
        return {
            "volume_db": self.volume_slider.value() / 10.0,
            "pitch_semitones": self.pitch_slider.value() / 1.0,
        }

    def reset(self) -> None:
        self._loading = True
        try:
            self.volume_slider.setValue(0)
            self.pitch_slider.setValue(0)
            self._refresh_labels()
        finally:
            self._loading = False
        self._emit_master()

    def _main_track(self) -> dict[str, Any]:
        return {
            "id": "main",
            "name": "原声",
            "role": "main",
            "asset_id": "main",
            "source_start": 0.0,
            "source_end": self._main_duration,
            "timeline_start": 0.0,
            "gain_db": 0.0,
            "muted": False,
            "solo": False,
            "loop": False,
            "fade_in": 0.0,
            "fade_out": 0.0,
        }

    def _next_id(self) -> str:
        existing = {item.get("id") for item in self._row_meta()}
        index = 1
        while f"t_{index:02d}" in existing:
            index += 1
        return f"t_{index:02d}"

    def _row_meta(self) -> list[dict]:
        metas: list[dict] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            meta = item.data(Qt.ItemDataRole.UserRole) if item else None
            if isinstance(meta, dict):
                metas.append(meta)
        return metas

    def _asset_label(self, asset_id: str) -> str:
        if asset_id == "main":
            return "原声"
        asset = self._assets.get(asset_id)
        return str(asset.get("name") or asset_id) if asset else asset_id

    def _populate_row(self, row: int, track: dict) -> None:
        asset_id = str(track.get("asset_id") or "main")
        meta = {
            "id": str(track.get("id") or f"t_{row + 1:02d}"),
            "role": str(track.get("role") or "other"),
            "asset_id": asset_id,
            "source_start": seconds_or_none(track.get("source_start")) or 0.0,
            "source_end": seconds_or_none(track.get("source_end")),
        }
        values = [
            str(track.get("name") or self._asset_label(asset_id)),
            self._asset_label(asset_id),
            f"{seconds_or_none(track.get('timeline_start')) or 0.0:.3f}",
            f"{float(track.get('gain_db') or 0.0):.1f}",
            "",
            "",
            "",
            f"{seconds_or_none(track.get('fade_in')) or 0.0:.2f}",
            f"{seconds_or_none(track.get('fade_out')) or 0.0:.2f}",
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 1:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setData(Qt.ItemDataRole.UserRole, meta)
            elif column in (4, 5, 6):
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                key = {4: "muted", 5: "solo", 6: "loop"}[column]
                item.setCheckState(
                    Qt.CheckState.Checked
                    if track.get(key)
                    else Qt.CheckState.Unchecked
                )
            self.table.setItem(row, column, item)

    def _collected(self) -> tuple[list[dict[str, Any]], list[str]]:
        tracks: list[dict[str, Any]] = []
        errors: list[str] = []
        for row in range(self.table.rowCount()):
            meta_item = self.table.item(row, 1)
            meta = meta_item.data(Qt.ItemDataRole.UserRole) if meta_item else None
            if not isinstance(meta, dict):
                errors.append(f"第 {row + 1} 行缺少音轨元数据")
                continue
            try:
                timeline_start = float(self.table.item(row, 2).text())
                gain_db = float(self.table.item(row, 3).text())
                fade_in = float(self.table.item(row, 7).text() or 0)
                fade_out = float(self.table.item(row, 8).text() or 0)
            except (AttributeError, TypeError, ValueError):
                errors.append(f"第 {row + 1} 行存在无效数字")
                continue
            name_item = self.table.item(row, 0)
            name = name_item.text().strip() if name_item else ""
            if timeline_start < 0:
                errors.append(f"第 {row + 1} 行起点不能为负")
                continue
            if not -60.0 <= gain_db <= 12.0:
                errors.append(f"第 {row + 1} 行增益超出 -60..12 dB")
                continue
            if fade_in < 0 or fade_out < 0:
                errors.append(f"第 {row + 1} 行淡入/淡出不能为负")
                continue
            asset_id = str(meta.get("asset_id") or "main")
            if asset_id != "main" and asset_id not in self._assets:
                errors.append(f"第 {row + 1} 行音频资产不存在")
                continue
            tracks.append(
                {
                    "id": str(meta.get("id") or f"t_{row + 1:02d}"),
                    "name": name or self._asset_label(asset_id),
                    "role": str(meta.get("role") or "other"),
                    "asset_id": asset_id,
                    "source_start": float(meta.get("source_start") or 0.0),
                    "source_end": meta.get("source_end"),
                    "timeline_start": timeline_start,
                    "gain_db": gain_db,
                    "muted": self.table.item(row, 4).checkState()
                    == Qt.CheckState.Checked,
                    "solo": self.table.item(row, 5).checkState()
                    == Qt.CheckState.Checked,
                    "loop": self.table.item(row, 6).checkState()
                    == Qt.CheckState.Checked,
                    "fade_in": fade_in,
                    "fade_out": fade_out,
                }
            )
        return tracks, errors

    def _collect(self) -> tuple[list[dict[str, Any]], list[str]]:
        return self._collected()

    def _on_item_changed(self, _item: QTableWidgetItem) -> None:
        if not self._loading:
            self._emit_tracks()

    def _emit_tracks(self) -> None:
        tracks, errors = self._collect()
        if errors:
            self.status_label.setText(errors[0])
            self.status_label.setStyleSheet("color:#f87171;")
            return
        self.status_label.setStyleSheet("")
        self._update_status(count=len(tracks))
        self.tracks_changed.emit(tracks)

    def _on_changed(self, _value: int) -> None:
        self._refresh_labels()
        if not self._loading:
            self._emit_master()

    def _refresh_labels(self) -> None:
        volume = self.volume_slider.value() / 10.0
        pitch = self.pitch_slider.value()
        self.volume_label.setText(f"{volume:+.1f} dB" if volume else "0.0 dB")
        self.pitch_label.setText(f"{pitch:+d} 半音" if pitch else "0 半音")

    def _emit_master(self) -> None:
        self.audio_changed.emit(self.audio())

    def _update_status(self, *, count: int | None = None) -> None:
        if count is None:
            count = self.table.rowCount()
        self.status_label.setStyleSheet("")
        self.status_label.setText(f"{count} 条音轨" if count else "无音轨（静音）")


def _duration(facts: dict) -> float:
    return seconds_or_none(facts.get("duration")) or 0.0
