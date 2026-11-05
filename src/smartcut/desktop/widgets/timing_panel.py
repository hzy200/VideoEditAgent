"""转场/变速/水印面板。

- 速度与转场改变时长；
- 文字/图片水印叠加不改变时长；
- 去水印使用 delogo 矩形区域（GUI 手动标注，模型不输出坐标）。
"""

from __future__ import annotations

import re
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from smartcut.contracts.time import RationalTime, seconds_from_mapping
from smartcut.contracts.timeline_ops import (
    TRANSITION_AUDIO_CURVES,
    TRANSITION_TYPES,
)

_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_POSITIONS = [
    ("左上", "top_left"),
    ("右上", "top_right"),
    ("左下", "bottom_left"),
    ("右下", "bottom_right"),
    ("居中", "center"),
]
_OVERLAY_KINDS = [("文字", "text"), ("图片/Logo", "image")]


def _to_rational(seconds: float) -> dict:
    return RationalTime.from_seconds(
        round(max(0.0, float(seconds)), 3), rate=90_000
    ).model_dump()


def _to_seconds(value) -> float:
    return seconds_from_mapping(value) or 0.0


class TimingPanel(QWidget):
    changed = Signal(object)
    overlay_import_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False
        self._segments: list[dict] = []
        self._overlay_assets: list[dict] = []
        self._speed_spins: list[tuple[str, QDoubleSpinBox]] = []
        self._transition_rows: list[dict] = []
        self._overlay_rows: list[dict] = []
        self._removal_rows: list[dict] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        hint = QLabel(
            "速度与转场改变成片时长；水印支持文字与图片/Logo；"
            "去水印为矩形 delogo 区域，坐标基于输出画面。"
        )
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        speed_group = QGroupBox("片段速度")
        speed_layout = QVBoxLayout(speed_group)
        self.speed_table = QTableWidget(0, 2)
        self.speed_table.setHorizontalHeaderLabels(["片段", "速度（0.5–2.0）"])
        self.speed_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.speed_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.speed_table.verticalHeader().setVisible(False)
        self.speed_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        speed_layout.addWidget(self.speed_table)
        layout.addWidget(speed_group)

        transition_group = QGroupBox("片段转场")
        transition_layout = QVBoxLayout(transition_group)
        self.transition_table = QTableWidget(0, 4)
        self.transition_table.setHorizontalHeaderLabels(
            ["片段", "类型", "时长(s，0=无)", "音频曲线"]
        )
        header = self.transition_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.transition_table.verticalHeader().setVisible(False)
        self.transition_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        transition_layout.addWidget(self.transition_table)
        layout.addWidget(transition_group)

        overlay_group = QGroupBox("水印")
        overlay_layout = QVBoxLayout(overlay_group)
        overlay_buttons = QHBoxLayout()
        self.add_overlay_button = QPushButton("添加水印")
        self.remove_overlay_button = QPushButton("删除选中")
        self.import_overlay_button = QPushButton("导入图片/Logo")
        overlay_buttons.addWidget(self.add_overlay_button)
        overlay_buttons.addWidget(self.remove_overlay_button)
        overlay_buttons.addWidget(self.import_overlay_button)
        overlay_buttons.addStretch(1)
        overlay_layout.addLayout(overlay_buttons)
        self.overlay_table = QTableWidget(0, 10)
        self.overlay_table.setHorizontalHeaderLabels(
            [
                "类型",
                "文本",
                "图片素材",
                "位置",
                "字号",
                "缩放",
                "颜色",
                "不透明度",
                "开始(s)",
                "结束(s，0=片尾)",
            ]
        )
        overlay_header = self.overlay_table.horizontalHeader()
        overlay_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        overlay_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        overlay_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column in range(3, 10):
            overlay_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.overlay_table.verticalHeader().setVisible(False)
        self.overlay_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.overlay_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        overlay_layout.addWidget(self.overlay_table)
        layout.addWidget(overlay_group)

        removal_group = QGroupBox("去水印（delogo 矩形区域）")
        removal_layout = QVBoxLayout(removal_group)
        removal_buttons = QHBoxLayout()
        self.add_removal_button = QPushButton("添加区域")
        self.remove_removal_button = QPushButton("删除选中")
        removal_buttons.addWidget(self.add_removal_button)
        removal_buttons.addWidget(self.remove_removal_button)
        removal_buttons.addStretch(1)
        removal_layout.addLayout(removal_buttons)
        self.removal_table = QTableWidget(0, 6)
        self.removal_table.setHorizontalHeaderLabels(
            ["X", "Y", "宽", "高", "开始(s)", "结束(s，0=片尾)"]
        )
        for column in range(6):
            self.removal_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.removal_table.verticalHeader().setVisible(False)
        self.removal_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.removal_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        removal_layout.addWidget(self.removal_table)
        layout.addWidget(removal_group)

        self.status_label = QLabel("尚无时间轴算子")
        self.status_label.setObjectName("mutedText")
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        self.add_overlay_button.clicked.connect(self.add_overlay)
        self.remove_overlay_button.clicked.connect(self.remove_selected_overlay)
        self.import_overlay_button.clicked.connect(
            self.overlay_import_requested.emit
        )
        self.add_removal_button.clicked.connect(self.add_removal)
        self.remove_removal_button.clicked.connect(self.remove_selected_removal)

    # ── 数据 ──────────────────────────────────────────────────────────
    def clear(self) -> None:
        self.set_data([], {}, [], [], [], [])

    def set_data(
        self,
        segments: list[dict],
        speeds: dict | None,
        transitions: list[dict] | None,
        overlays: list[dict] | None,
        removals: list[dict] | None = None,
        overlay_assets: list[dict] | None = None,
    ) -> None:
        self._segments = [
            {"id": segment.get("id"), "tag": segment.get("tag") or ""}
            for segment in (segments or [])
            if segment.get("id")
        ]
        if overlay_assets is not None:
            self._overlay_assets = list(overlay_assets)
        speed_map = {
            str(key): float(value) for key, value in (speeds or {}).items()
        }
        transition_map = {
            str(item.get("after")): item for item in (transitions or [])
        }
        self._loading = True
        try:
            self._populate_speeds(speed_map)
            self._populate_transitions(transition_map)
            self._populate_overlays(list(overlays or []))
            self._populate_removals(list(removals or []))
        finally:
            self._loading = False
        self.status_label.setText(
            f"{len(self._segments)} 段 · "
            f"{len(self._transition_rows)} 个转场 · "
            f"{len(self._overlay_rows)} 个水印 · "
            f"{len(self._removal_rows)} 个去水印区域"
        )

    def set_overlay_assets(self, assets: list[dict] | None) -> None:
        self._overlay_assets = list(assets or [])
        self._loading = True
        try:
            for row in self._overlay_rows:
                self._fill_asset_combo(row["asset"], row["asset"].currentData())
        finally:
            self._loading = False

    def payload(self) -> dict:
        speeds = {
            segment_id: max(0.5, min(2.0, float(spin.value())))
            for segment_id, spin in self._speed_spins
        }
        transitions: list[dict] = []
        for row in self._transition_rows:
            duration = float(row["duration"].value())
            if duration <= 0:
                continue
            transitions.append(
                {
                    "after": row["after"],
                    "type": row["type"].currentData(),
                    "duration": _to_rational(duration),
                    "audio_curve": (
                        row["audio_curve"].currentData()
                        if "audio_curve" in row
                        else "tri"
                    ),
                }
            )
        overlays: list[dict] = []
        for row in self._overlay_rows:
            kind = row["kind"].currentData()
            start = float(row["start"].value())
            end = float(row["end"].value())
            base = {
                "kind": kind,
                "position": row["position"].currentData(),
                "opacity": float(row["opacity"].value()),
                "start": _to_rational(start),
                "end": _to_rational(end) if end > 0 else None,
            }
            if kind == "image":
                base.update(
                    {
                        "asset_id": row["asset"].currentData(),
                        "scale": float(row["scale"].value()),
                    }
                )
            else:
                color = row["color"].text().strip().upper()
                base.update(
                    {
                        "text": row["text"].text().strip(),
                        "font_size": int(row["size"].value()),
                        "color": color if _COLOR_RE.fullmatch(color) else "#FFFFFF",
                    }
                )
            overlays.append(base)
        removals: list[dict] = []
        for row in self._removal_rows:
            start = float(row["start"].value())
            end = float(row["end"].value())
            removals.append(
                {
                    "id": row["id"],
                    "x": int(row["x"].value()),
                    "y": int(row["y"].value()),
                    "width": int(row["width"].value()),
                    "height": int(row["height"].value()),
                    "start": _to_rational(start),
                    "end": _to_rational(end) if end > 0 else None,
                }
            )
        return {
            "speeds": speeds,
            "transitions": transitions,
            "overlays": overlays,
            "removals": removals,
        }

    def errors(self) -> list[str]:
        errors: list[str] = []
        for index, row in enumerate(self._overlay_rows, start=1):
            kind = row["kind"].currentData()
            if kind == "text":
                if not row["text"].text().strip():
                    errors.append(f"水印 {index} 文本不能为空")
                color = row["color"].text().strip()
                if not _COLOR_RE.fullmatch(color):
                    errors.append(f"水印 {index} 颜色必须是 #RRGGBB")
            else:
                if not row["asset"].currentData():
                    errors.append(f"水印 {index} 未选择图片素材")
            start = float(row["start"].value())
            end = float(row["end"].value())
            if end > 0 and end <= start:
                errors.append(f"水印 {index} 结束时间必须大于开始时间")
        for index, row in enumerate(self._removal_rows, start=1):
            start = float(row["start"].value())
            end = float(row["end"].value())
            if end > 0 and end <= start:
                errors.append(f"去水印区域 {index} 结束时间必须大于开始时间")
        return errors

    # ── 速度/转场 ────────────────────────────────────────────────────
    def _populate_speeds(self, speed_map: dict[str, float]) -> None:
        self.speed_table.blockSignals(True)
        self.speed_table.setRowCount(len(self._segments))
        self._speed_spins = []
        for row, segment in enumerate(self._segments):
            tag = str(segment.get("tag") or "").strip()
            label = f"片段 {row + 1}" + (f" · {tag[:16]}" if tag else "")
            item = QTableWidgetItem(label)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.speed_table.setItem(row, 0, item)
            spin = QDoubleSpinBox()
            spin.setRange(0.5, 2.0)
            spin.setSingleStep(0.1)
            spin.setDecimals(2)
            spin.setValue(speed_map.get(segment["id"], 1.0))
            spin.valueChanged.connect(lambda _value: self._emit())
            self.speed_table.setCellWidget(row, 1, spin)
            self._speed_spins.append((segment["id"], spin))
        self.speed_table.blockSignals(False)

    def _populate_transitions(self, transition_map: dict[str, dict]) -> None:
        count = max(0, len(self._segments) - 1)
        self.transition_table.blockSignals(True)
        self.transition_table.setRowCount(count)
        self._transition_rows = []
        for row in range(count):
            after = self._segments[row]["id"]
            spec = transition_map.get(after) or {}
            item = QTableWidgetItem(f"片段 {row + 1} → {row + 2}")
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.transition_table.setItem(row, 0, item)
            combo = QComboBox()
            for kind in TRANSITION_TYPES:
                combo.addItem(kind, kind)
            combo.setCurrentIndex(
                max(0, combo.findData(spec.get("type") or "fade"))
            )
            combo.currentIndexChanged.connect(lambda _index: self._emit())
            self.transition_table.setCellWidget(row, 1, combo)
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 5.0)
            spin.setSingleStep(0.1)
            spin.setDecimals(2)
            spin.setValue(_to_seconds(spec.get("duration")))
            spin.valueChanged.connect(lambda _value: self._emit())
            self.transition_table.setCellWidget(row, 2, spin)
            curve = QComboBox()
            for value in TRANSITION_AUDIO_CURVES:
                curve.addItem(value, value)
            curve.setCurrentIndex(
                max(0, curve.findData(spec.get("audio_curve") or "tri"))
            )
            curve.currentIndexChanged.connect(lambda _index: self._emit())
            self.transition_table.setCellWidget(row, 3, curve)
            self._transition_rows.append(
                {
                    "after": after,
                    "type": combo,
                    "duration": spin,
                    "audio_curve": curve,
                }
            )
        self.transition_table.blockSignals(False)

    # ── 水印 ──────────────────────────────────────────────────────────
    def _fill_asset_combo(self, combo: QComboBox, selected: str | None) -> None:
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("（未选择）", None)
            for asset in self._overlay_assets:
                combo.addItem(
                    str(asset.get("name") or asset.get("id")),
                    asset.get("id"),
                )
            if selected and combo.findData(selected) < 0:
                combo.addItem("（资产缺失）", selected)
            index = combo.findData(selected) if selected else 0
            combo.setCurrentIndex(max(0, index))
        finally:
            combo.blockSignals(False)

    def _populate_overlays(self, overlays: list[dict]) -> None:
        self.overlay_table.setRowCount(0)
        self._overlay_rows = []
        for overlay in overlays:
            self._append_overlay_row(overlay)

    def _append_overlay_row(self, overlay: dict) -> None:
        row = self.overlay_table.rowCount()
        self.overlay_table.insertRow(row)

        kind = QComboBox()
        for label, value in _OVERLAY_KINDS:
            kind.addItem(label, value)
        kind.setCurrentIndex(
            max(0, kind.findData(overlay.get("kind") or "text"))
        )
        self.overlay_table.setCellWidget(row, 0, kind)

        text = QLineEdit(str(overlay.get("text") or "水印"))
        text.editingFinished.connect(self._emit)
        self.overlay_table.setCellWidget(row, 1, text)

        asset = QComboBox()
        self._fill_asset_combo(asset, overlay.get("asset_id"))
        asset.currentIndexChanged.connect(lambda _index: self._emit())
        self.overlay_table.setCellWidget(row, 2, asset)

        position = QComboBox()
        for label, value in _POSITIONS:
            position.addItem(label, value)
        position.setCurrentIndex(
            max(0, position.findData(overlay.get("position") or "bottom_right"))
        )
        position.currentIndexChanged.connect(lambda _index: self._emit())
        self.overlay_table.setCellWidget(row, 3, position)

        size = QSpinBox()
        size.setRange(8, 120)
        size.setValue(int(overlay.get("font_size") or 28))
        size.valueChanged.connect(lambda _value: self._emit())
        self.overlay_table.setCellWidget(row, 4, size)

        scale = QDoubleSpinBox()
        scale.setRange(0.02, 1.0)
        scale.setSingleStep(0.01)
        scale.setDecimals(2)
        scale.setValue(float(overlay.get("scale") or 0.2))
        scale.valueChanged.connect(lambda _value: self._emit())
        self.overlay_table.setCellWidget(row, 5, scale)

        color = QLineEdit(str(overlay.get("color") or "#FFFFFF"))
        color.editingFinished.connect(self._emit)
        self.overlay_table.setCellWidget(row, 6, color)

        opacity = QDoubleSpinBox()
        opacity.setRange(0.0, 1.0)
        opacity.setSingleStep(0.05)
        opacity.setDecimals(2)
        opacity.setValue(float(overlay.get("opacity") or 0.7))
        opacity.valueChanged.connect(lambda _value: self._emit())
        self.overlay_table.setCellWidget(row, 7, opacity)

        start = QDoubleSpinBox()
        start.setRange(0.0, 36000.0)
        start.setSingleStep(0.5)
        start.setDecimals(1)
        start.setValue(_to_seconds(overlay.get("start")))
        start.valueChanged.connect(lambda _value: self._emit())
        self.overlay_table.setCellWidget(row, 8, start)

        end = QDoubleSpinBox()
        end.setRange(0.0, 36000.0)
        end.setSingleStep(0.5)
        end.setDecimals(1)
        end.setValue(_to_seconds(overlay.get("end")))
        end.valueChanged.connect(lambda _value: self._emit())
        self.overlay_table.setCellWidget(row, 9, end)

        row_widgets = {
            "kind": kind,
            "text": text,
            "asset": asset,
            "position": position,
            "size": size,
            "scale": scale,
            "color": color,
            "opacity": opacity,
            "start": start,
            "end": end,
        }
        self._overlay_rows.append(row_widgets)
        kind.currentIndexChanged.connect(
            lambda _index, widgets=row_widgets: self._toggle_overlay_kind(widgets)
        )
        self._toggle_overlay_kind(row_widgets)

    def _toggle_overlay_kind(self, row: dict) -> None:
        image = row["kind"].currentData() == "image"
        row["text"].setEnabled(not image)
        row["size"].setEnabled(not image)
        row["color"].setEnabled(not image)
        row["asset"].setEnabled(image)
        row["scale"].setEnabled(image)

    def add_overlay(self) -> None:
        current = self.payload()["overlays"]
        current.append({"kind": "text", "text": "水印"})
        self._loading = True
        try:
            self._populate_overlays(current)
        finally:
            self._loading = False
        self._emit()

    def remove_selected_overlay(self) -> None:
        row = self.overlay_table.currentRow()
        if row < 0:
            return
        current = self.payload()["overlays"]
        if 0 <= row < len(current):
            current.pop(row)
        self._loading = True
        try:
            self._populate_overlays(current)
        finally:
            self._loading = False
        self._emit()

    # ── 去水印 ────────────────────────────────────────────────────────
    def _populate_removals(self, removals: list[dict]) -> None:
        self.removal_table.setRowCount(0)
        self._removal_rows = []
        for removal in removals:
            self._append_removal_row(removal)

    def _append_removal_row(self, removal: dict) -> None:
        row = self.removal_table.rowCount()
        self.removal_table.insertRow(row)
        x = QSpinBox()
        x.setRange(1, 16384)
        x.setValue(max(1, int(removal.get("x") or 10)))
        y = QSpinBox()
        y.setRange(1, 16384)
        y.setValue(max(1, int(removal.get("y") or 10)))
        width = QSpinBox()
        width.setRange(1, 16384)
        width.setValue(max(1, int(removal.get("width") or 120)))
        height = QSpinBox()
        height.setRange(1, 16384)
        height.setValue(max(1, int(removal.get("height") or 60)))
        start = QDoubleSpinBox()
        start.setRange(0.0, 36000.0)
        start.setSingleStep(0.5)
        start.setDecimals(1)
        start.setValue(_to_seconds(removal.get("start")))
        end = QDoubleSpinBox()
        end.setRange(0.0, 36000.0)
        end.setSingleStep(0.5)
        end.setDecimals(1)
        end.setValue(_to_seconds(removal.get("end")))
        for column, widget in enumerate(
            (x, y, width, height, start, end)
        ):
            if hasattr(widget, "valueChanged"):
                widget.valueChanged.connect(lambda _value: self._emit())
            self.removal_table.setCellWidget(row, column, widget)
        self._removal_rows.append(
            {
                "id": str(removal.get("id") or f"rm_{uuid4().hex[:8]}"),
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "start": start,
                "end": end,
            }
        )

    def add_removal(self) -> None:
        current = self.payload()["removals"]
        current.append(
            {
                "id": f"rm_{uuid4().hex[:8]}",
                "x": 10,
                "y": 10,
                "width": 120,
                "height": 60,
            }
        )
        self._loading = True
        try:
            self._populate_removals(current)
        finally:
            self._loading = False
        self._emit()

    def remove_selected_removal(self) -> None:
        row = self.removal_table.currentRow()
        if row < 0:
            return
        current = self.payload()["removals"]
        if 0 <= row < len(current):
            current.pop(row)
        self._loading = True
        try:
            self._populate_removals(current)
        finally:
            self._loading = False
        self._emit()

    def _emit(self) -> None:
        if self._loading:
            return
        self.status_label.setText("时间轴算子已修改（未保存）")
        self.changed.emit(self.payload())
