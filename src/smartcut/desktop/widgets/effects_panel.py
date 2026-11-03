"""特效面板：手动添加/编辑/删除全局特效，与 Planner 共用同一 EditIR 词汇。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from pydantic import TypeAdapter

from smartcut.contracts.effects import Effect
from smartcut.contracts.time import RationalTime, seconds_from_mapping

_EFFECT_ADAPTER = TypeAdapter(Effect)

_TYPE_LABELS = {
    "fit": "竖版 9:16",
    "color": "调色",
    "blur": "模糊",
    "sharpen": "锐化",
    "vignette": "暗角",
    "fade": "淡入淡出",
    "zoom": "缓推",
}

_PRESETS: dict[str, list[dict]] = {
    "无特效": [],
    "电影感": [
        {"type": "color", "contrast": 1.08, "saturation": 0.92, "temperature": -8},
        {"type": "vignette", "angle": 0.45},
    ],
    "黑白": [{"type": "color", "saturation": 0.0}],
    "轻微暗角": [{"type": "vignette", "angle": 0.4}],
    "开场淡入": [
        {
            "type": "fade",
            "fade_in": RationalTime.from_seconds(1, rate=90_000).model_dump(),
        }
    ],
    "轻微推近": [{"type": "zoom", "start_scale": 1.0, "end_scale": 1.08}],
}

_DEFAULTS: dict[str, dict] = {
    "fit": {"type": "fit"},
    "color": {"type": "color"},
    "blur": {"type": "blur", "radius": 2.0},
    "sharpen": {"type": "sharpen", "amount": 0.6},
    "vignette": {"type": "vignette", "angle": 0.4},
    "fade": {
        "type": "fade",
        "fade_in": RationalTime.from_seconds(1, rate=90_000).model_dump(),
        "fade_out": RationalTime.from_seconds(1, rate=90_000).model_dump(),
    },
    "zoom": {"type": "zoom", "start_scale": 1.0, "end_scale": 1.1},
}


def _normalize(effect: dict) -> dict:
    return _EFFECT_ADAPTER.validate_python(effect).model_dump(mode="json")


def _effect_seconds(value: Any) -> float:
    return seconds_from_mapping(value) or 0.0


def _value(effect: dict, field: str, default: float) -> float:
    value = effect.get(field)
    return default if value is None else float(value)


class EffectsPanel(QWidget):
    effects_changed = Signal(object)
    preview_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._effects: list[dict] = []
        self._segments: list[dict] = []
        self._loading = False
        self._invalid = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        hint = QLabel(
            "特效只改变画面，不改变时长；参数编辑立即写入草稿，"
            "“预览特效”会渲染 480p 全片代理，最终渲染仍按原分辨率。"
        )
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        preset_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(_PRESETS))
        self.apply_preset_button = QPushButton("应用预设")
        preset_row.addWidget(QLabel("预设"))
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(self.apply_preset_button)
        layout.addLayout(preset_row)

        scope_row = QHBoxLayout()
        self.add_scope_combo = QComboBox()
        self.add_scope_combo.addItem("全局（整片）", None)
        scope_row.addWidget(QLabel("添加作用域"))
        scope_row.addWidget(self.add_scope_combo, 1)
        layout.addLayout(scope_row)

        add_row = QHBoxLayout()
        self.type_combo = QComboBox()
        for key, label in _TYPE_LABELS.items():
            self.type_combo.addItem(label, key)
        self.add_button = QPushButton("添加特效")
        self.remove_button = QPushButton("删除选中")
        self.up_button = QPushButton("上移")
        self.down_button = QPushButton("下移")
        self.preview_button = QPushButton("预览特效")
        add_row.addWidget(self.type_combo, 1)
        add_row.addWidget(self.add_button)
        add_row.addWidget(self.remove_button)
        add_row.addWidget(self.up_button)
        add_row.addWidget(self.down_button)
        add_row.addStretch(1)
        add_row.addWidget(self.preview_button)
        layout.addLayout(add_row)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["启用", "作用域", "类型", "参数"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)

        editor_group = QGroupBox("参数")
        self.editor_form = QFormLayout(editor_group)
        layout.addWidget(editor_group)

        self.status_label = QLabel("尚无特效")
        self.status_label.setObjectName("mutedText")
        layout.addWidget(self.status_label)

        self.add_button.clicked.connect(self.add_current_type)
        self.remove_button.clicked.connect(self.remove_selected)
        self.up_button.clicked.connect(lambda: self.move_selected(-1))
        self.down_button.clicked.connect(lambda: self.move_selected(1))
        self.apply_preset_button.clicked.connect(self.apply_preset)
        self.preview_button.clicked.connect(self.preview_requested.emit)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self._update_buttons()

    # ── 数据 ──────────────────────────────────────────────────────────
    def clear(self) -> None:
        self._loading = True
        try:
            self._effects = []
            self._segments = []
            self._invalid = 0
            self.table.setRowCount(0)
            self._clear_editor()
            self._rebuild_scope_combo()
        finally:
            self._loading = False
        self.status_label.setText("尚无特效")
        self._update_buttons()

    def set_segments(self, segments: list[dict] | None) -> None:
        """由 MainWindow 提供当前时间线片段，用于逐片段作用域选择。"""
        updated = [
            {"id": segment.get("id"), "tag": segment.get("tag") or ""}
            for segment in (segments or [])
            if segment.get("id")
        ]
        if updated == self._segments:
            return
        self._segments = updated
        self._rebuild_scope_combo()
        self._refresh_scope_column()
        row = self.table.currentRow()
        if 0 <= row < len(self._effects):
            self._rebuild_editor(row)

    def set_effects(self, effects: list[dict] | None) -> None:
        valid: list[dict] = []
        invalid = 0
        for effect in effects or []:
            try:
                valid.append(_normalize(effect))
            except Exception:
                invalid += 1
        self._loading = True
        try:
            self._effects = valid
            self._invalid = invalid
            self._populate()
        finally:
            self._loading = False
        self.status_label.setText(
            f"{len(valid)} 个特效"
            + (f"（{invalid} 个无法识别已跳过）" if invalid else "")
        )
        self._update_buttons()

    def effects(self) -> list[dict]:
        return deepcopy(self._effects)

    def errors(self) -> list[str]:
        return []

    # ── 编辑 ──────────────────────────────────────────────────────────
    def add_current_type(self) -> None:
        key = str(self.type_combo.currentData() or "color")
        scope_id = self.add_scope_combo.currentData()
        raw = dict(_DEFAULTS.get(key) or {"type": key})
        if scope_id:
            raw.update(target="segment", segment_id=str(scope_id))
        try:
            effect = _normalize(raw)
        except Exception as exc:
            self.status_label.setText(f"无法添加特效: {exc}")
            return
        self._effects.append(effect)
        self._loading = True
        try:
            self._populate(selected=len(self._effects) - 1)
        finally:
            self._loading = False
        self._emit()

    def remove_selected(self) -> None:
        row = self.table.currentRow()
        if not (0 <= row < len(self._effects)):
            return
        self._effects.pop(row)
        self._loading = True
        try:
            self._populate(selected=min(row, len(self._effects) - 1))
        finally:
            self._loading = False
        self._emit()

    def move_selected(self, delta: int) -> None:
        row = self.table.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= len(self._effects):
            return
        self._effects[row], self._effects[target] = (
            self._effects[target],
            self._effects[row],
        )
        self._loading = True
        try:
            self._populate(selected=target)
        finally:
            self._loading = False
        self._emit()

    def apply_preset(self) -> None:
        name = self.preset_combo.currentText()
        try:
            effects = [_normalize(item) for item in _PRESETS.get(name, [])]
        except Exception as exc:
            self.status_label.setText(f"预设无效: {exc}")
            return
        self._effects = effects
        self._loading = True
        try:
            self._populate(selected=0 if effects else None)
        finally:
            self._loading = False
        self._emit()

    # ── 表格/参数 ─────────────────────────────────────────────────────
    def _populate(self, selected: int | None = None) -> None:
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(self._effects))
            for row, effect in enumerate(self._effects):
                enabled = QTableWidgetItem()
                enabled.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsSelectable
                )
                enabled.setCheckState(
                    Qt.CheckState.Checked
                    if effect.get("enabled", True)
                    else Qt.CheckState.Unchecked
                )
                self.table.setItem(row, 0, enabled)
                scope_item = QTableWidgetItem(self._effect_scope_text(effect))
                scope_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                )
                self.table.setItem(row, 1, scope_item)
                type_item = QTableWidgetItem(
                    _TYPE_LABELS.get(str(effect.get("type")), str(effect.get("type")))
                )
                type_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                )
                self.table.setItem(row, 2, type_item)
                summary = QTableWidgetItem(self._summary(effect))
                summary.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                )
                self.table.setItem(row, 3, summary)
            if selected is not None and 0 <= selected < len(self._effects):
                self.table.setCurrentCell(selected, 0)
            elif self._effects:
                self.table.setCurrentCell(0, 0)
        finally:
            self.table.blockSignals(False)
        self._rebuild_scope_combo()
        self._rebuild_editor(self.table.currentRow())
        self.status_label.setText(f"{len(self._effects)} 个特效")
        self._update_buttons()

    def _update_buttons(self) -> None:
        row = self.table.currentRow()
        self.remove_button.setEnabled(0 <= row < len(self._effects))
        self.up_button.setEnabled(0 < row < len(self._effects))
        self.down_button.setEnabled(0 <= row < len(self._effects) - 1)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != 0:
            return
        row = item.row()
        if not (0 <= row < len(self._effects)):
            return
        self._effects[row]["enabled"] = (
            item.checkState() == Qt.CheckState.Checked
        )
        self._rebuild_editor(row)
        self._emit()

    def _on_selection_changed(self) -> None:
        self._rebuild_editor(self.table.currentRow())
        self._update_buttons()

    def _fill_scope_combo(self, combo: QComboBox, selected_id: str | None) -> None:
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("全局（整片）", None)
            for index, segment in enumerate(self._segments):
                label = f"片段 {index + 1}"
                tag = str(segment.get("tag") or "").strip()
                if tag:
                    label += f" · {tag[:16]}"
                combo.addItem(label, segment.get("id"))
            if selected_id and combo.findData(selected_id) < 0:
                combo.addItem("片段已删除", selected_id)
            position = combo.findData(selected_id) if selected_id else 0
            combo.setCurrentIndex(max(0, position))
        finally:
            combo.blockSignals(False)

    def _rebuild_scope_combo(self) -> None:
        self._fill_scope_combo(
            self.add_scope_combo, self.add_scope_combo.currentData()
        )

    def _set_effect_scope(self, row: int, segment_id: str | None) -> None:
        if self._loading or not (0 <= row < len(self._effects)):
            return
        effect = self._effects[row]
        if segment_id:
            effect["target"] = "segment"
            effect["segment_id"] = str(segment_id)
        else:
            effect["target"] = "output"
            effect["segment_id"] = None
        self._refresh_row(row)
        self._emit()

    def _effect_scope_text(self, effect: dict) -> str:
        if effect.get("target") != "segment":
            return "全局"
        segment_id = effect.get("segment_id")
        for index, segment in enumerate(self._segments):
            if segment.get("id") == segment_id:
                tag = str(segment.get("tag") or "").strip()
                return f"片段 {index + 1}" + (f" · {tag[:12]}" if tag else "")
        return "片段已删除"

    def _refresh_scope_column(self) -> None:
        for row, effect in enumerate(self._effects):
            item = self.table.item(row, 1)
            if item is not None:
                item.setText(self._effect_scope_text(effect))

    def _clear_editor(self) -> None:
        while self.editor_form.rowCount():
            self.editor_form.removeRow(0)

    def _add_row(self, label: str, widget: QWidget) -> None:
        self.editor_form.addRow(label, widget)

    def _rebuild_editor(self, row: int) -> None:
        self._clear_editor()
        if not (0 <= row < len(self._effects)):
            self.editor_form.addRow(QLabel("选择一行特效后可编辑参数"))
            return
        effect = self._effects[row]
        kind = str(effect.get("type") or "")
        enabled = QLabel("已启用" if effect.get("enabled", True) else "已禁用")
        self._add_row("状态", enabled)

        scope = QComboBox()
        selected_id = (
            effect.get("segment_id")
            if effect.get("target") == "segment"
            else None
        )
        self._fill_scope_combo(scope, selected_id)
        scope.currentIndexChanged.connect(
            lambda _index, widget=scope, row_index=row: self._set_effect_scope(
                row_index, widget.currentData()
            )
        )
        self._add_row("作用域", scope)

        if kind == "fit":
            combo = QComboBox()
            combo.addItem("人脸优先", "face_bbox")
            combo.addItem("中心裁切", "center")
            index = combo.findData(str(effect.get("anchor") or "face_bbox"))
            combo.setCurrentIndex(max(0, index))
            combo.currentIndexChanged.connect(
                lambda _idx, widget=combo: self._set_param("anchor", widget.currentData())
            )
            self._add_row("锚点", combo)
        elif kind == "color":
            self._spin("brightness", "亮度", effect, -1.0, 1.0, 0.01, 2)
            self._spin("contrast", "对比度", effect, 0.0, 3.0, 0.01, 2)
            self._spin("saturation", "饱和度", effect, 0.0, 3.0, 0.01, 2)
            self._spin("gamma", "Gamma", effect, 0.1, 10.0, 0.01, 2)
            spin = QSpinBox()
            spin.setRange(-100, 100)
            spin.setValue(int(effect.get("temperature") or 0))
            spin.setSuffix(" （暖/冷）")
            spin.valueChanged.connect(
                lambda value: self._set_param("temperature", int(value))
            )
            self._add_row("色温", spin)
        elif kind == "blur":
            self._spin("radius", "半径", effect, 0.0, 50.0, 0.1, 1)
        elif kind == "sharpen":
            self._spin("amount", "强度", effect, 0.0, 2.0, 0.01, 2)
        elif kind == "vignette":
            self._spin("angle", "角度", effect, 0.0, 1.57, 0.01, 2)
        elif kind == "fade":
            self._spin(
                "fade_in", "淡入(s)", effect, 0.0, 5.0, 0.1, 1, seconds=True
            )
            self._spin(
                "fade_out", "淡出(s)", effect, 0.0, 5.0, 0.1, 1, seconds=True
            )
        elif kind == "zoom":
            self._spin("start_scale", "起始缩放", effect, 1.0, 2.0, 0.01, 2)
            self._spin("end_scale", "结束缩放", effect, 1.0, 2.0, 0.01, 2)
        else:
            self.editor_form.addRow(QLabel(f"未知特效类型: {kind}"))

    def _spin(
        self,
        field: str,
        label: str,
        effect: dict,
        minimum: float,
        maximum: float,
        step: float,
        decimals: int,
        *,
        seconds: bool = False,
    ) -> None:
        widget = QDoubleSpinBox()
        widget.setRange(minimum, maximum)
        widget.setSingleStep(step)
        widget.setDecimals(decimals)
        value = (
            _effect_seconds(effect.get(field))
            if seconds
            else float(effect.get(field) or 0.0)
        )
        widget.setValue(value)
        if seconds:
            widget.valueChanged.connect(
                lambda value: self._set_seconds(field, float(value))
            )
        else:
            widget.valueChanged.connect(
                lambda value: self._set_param(field, float(value))
            )
        self._add_row(label, widget)

    def _set_param(self, field: str, value: Any) -> None:
        if self._loading:
            return
        row = self.table.currentRow()
        if not (0 <= row < len(self._effects)):
            return
        self._effects[row][field] = value
        self._refresh_row(row)
        self._emit()

    def _set_seconds(self, field: str, seconds: float) -> None:
        if self._loading:
            return
        row = self.table.currentRow()
        if not (0 <= row < len(self._effects)):
            return
        self._effects[row][field] = RationalTime.from_seconds(
            round(max(0.0, seconds), 3), rate=90_000
        ).model_dump()
        self._refresh_row(row)
        self._emit()

    def _refresh_row(self, row: int) -> None:
        effect = self._effects[row]
        scope_item = self.table.item(row, 1)
        if scope_item is not None:
            scope_item.setText(self._effect_scope_text(effect))
        item = self.table.item(row, 3)
        if item is not None:
            item.setText(self._summary(effect))

    def _emit(self) -> None:
        if self._loading:
            return
        self.status_label.setText(f"{len(self._effects)} 个特效（未保存）")
        self.effects_changed.emit(self.effects())

    @staticmethod
    def _summary(effect: dict) -> str:
        kind = str(effect.get("type") or "")
        if kind == "fit":
            anchor = "人脸" if effect.get("anchor") == "face_bbox" else "中心"
            return f"9:16 · {anchor}"
        if kind == "color":
            parts = []
            if effect.get("brightness"):
                parts.append(f"亮度{float(effect['brightness']):+.2f}")
            contrast = _value(effect, "contrast", 1.0)
            if contrast != 1.0:
                parts.append(f"对比{contrast:.2f}")
            saturation = _value(effect, "saturation", 1.0)
            if saturation != 1.0:
                parts.append(f"饱和{saturation:.2f}")
            gamma = _value(effect, "gamma", 1.0)
            if gamma != 1.0:
                parts.append(f"gamma{gamma:.2f}")
            if effect.get("temperature"):
                parts.append(f"色温{int(effect['temperature']):+d}")
            return " · ".join(parts) or "默认"
        if kind == "blur":
            return f"半径 {float(effect.get('radius') or 0):.1f}"
        if kind == "sharpen":
            return f"强度 {float(effect.get('amount') or 0):.2f}"
        if kind == "vignette":
            return f"角度 {float(effect.get('angle') or 0):.2f}"
        if kind == "fade":
            return (
                f"淡入 {_effect_seconds(effect.get('fade_in')):.1f}s · "
                f"淡出 {_effect_seconds(effect.get('fade_out')):.1f}s"
            )
        if kind == "zoom":
            return (
                f"{_value(effect, 'start_scale', 1.0):.2f} → "
                f"{_value(effect, 'end_scale', 1.0):.2f}"
            )
        return kind
