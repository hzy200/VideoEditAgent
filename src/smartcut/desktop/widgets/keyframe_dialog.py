"""关键帧编辑器：逐点编辑/删除 PiP transform 关键帧。"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from smartcut.contracts.time import RationalTime, seconds_from_mapping

_COLUMNS = ("t(s)", "X", "Y", "宽", "高", "透明度")
_CURVE_COLORS = {
    "x": QColor("#38bdf8"),
    "y": QColor("#a78bfa"),
    "width": QColor("#34d399"),
    "height": QColor("#fbbf24"),
    "opacity": QColor("#f472b6"),
}


def _time_seconds(value) -> float:
    if isinstance(value, dict):
        return seconds_from_mapping(value) or 0.0
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class KeyframeCurveWidget(QWidget):
    """关键帧属性曲线预览（x/y/宽/高/透明度）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.keyframes: list[dict] = []
        self.keys = ("x", "y", "width", "height", "opacity")
        self.setMinimumHeight(120)
        self.setObjectName("mediaCanvas")

    def set_keyframes(self, keyframes: list[dict]) -> None:
        self.keyframes = list(keyframes or [])
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect().adjusted(8, 8, -8, -8))
        painter.setPen(QPen(QColor("#1e293b"), 1))
        for fraction in (0.0, 0.5, 1.0):
            y = rect.top() + rect.height() * fraction
            painter.drawLine(rect.left(), y, rect.right(), y)
        if len(self.keyframes) < 1:
            painter.setPen(QColor("#64748b"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "暂无关键帧")
            return
        points = sorted(
            self.keyframes, key=lambda item: _time_seconds(item.get("t"))
        )
        max_time = max(1e-6, _time_seconds(points[-1].get("t")))
        for key in self.keys:
            color = _CURVE_COLORS[key]
            painter.setPen(QPen(color, 1.5))
            previous = None
            for item in points:
                x = rect.left() + rect.width() * (
                    _time_seconds(item.get("t")) / max_time
                )
                value = max(0.0, min(1.0, float(item.get(key, 0.0))))
                y = rect.bottom() - rect.height() * value
                if previous is not None:
                    painter.drawLine(previous[0], previous[1], x, y)
                painter.setBrush(color)
                painter.drawEllipse(QRectF(x - 2.5, y - 2.5, 5, 5))
                previous = (x, y)
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(
            QRectF(rect.left(), rect.bottom() + 2, rect.width(), 12),
            Qt.AlignmentFlag.AlignLeft,
            "x  y  宽  高  透明度",
        )


class KeyframeDialog(QDialog):
    def __init__(self, keyframes: list[dict] | None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("画中画关键帧")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(_COLUMNS))
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        self.curve = KeyframeCurveWidget()
        layout.addWidget(self.curve)
        row = QHBoxLayout()
        add_button = QPushButton("添加")
        delete_button = QPushButton("删除")
        add_button.clicked.connect(self._add_row)
        delete_button.clicked.connect(self._delete_row)
        row.addWidget(add_button)
        row.addWidget(delete_button)
        row.addStretch(1)
        layout.addLayout(row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.table.itemChanged.connect(self._refresh_curve)
        for keyframe in keyframes or []:
            self._insert_keyframe(keyframe)
        self._refresh_curve()

    def _refresh_curve(self, *_args) -> None:
        payload = []
        for row in range(self.table.rowCount()):
            try:
                values = [
                    float(self.table.item(row, column).text())
                    for column in range(len(_COLUMNS))
                ]
            except (AttributeError, TypeError, ValueError):
                continue
            payload.append(
                {
                    "t": values[0],
                    "x": values[1],
                    "y": values[2],
                    "width": values[3],
                    "height": values[4],
                    "opacity": values[5],
                }
            )
        self.curve.set_keyframes(payload)

    def _insert_keyframe(self, keyframe: dict) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = (
            f"{_time_seconds(keyframe.get('t')):.3f}",
            f"{float(keyframe.get('x', 0.0)):.3f}",
            f"{float(keyframe.get('y', 0.0)):.3f}",
            f"{float(keyframe.get('width', 1.0)):.3f}",
            f"{float(keyframe.get('height', 1.0)):.3f}",
            f"{float(keyframe.get('opacity', 1.0)):.3f}",
        )
        for column, value in enumerate(values):
            self.table.setItem(row, column, QTableWidgetItem(value))

    def _add_row(self) -> None:
        self._insert_keyframe(
            {"t": 0.0, "x": 0.65, "y": 0.05, "width": 0.3, "height": 0.3, "opacity": 1.0}
        )
        self._refresh_curve()

    def _delete_row(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)
            self._refresh_curve()

    def accept(self) -> None:  # noqa: D401
        try:
            self.keyframes()
        except ValueError as exc:
            QMessageBox.warning(self, "关键帧参数错误", str(exc))
            return
        super().accept()

    def keyframes(self) -> list[dict]:
        result: list[dict] = []
        for row in range(self.table.rowCount()):
            try:
                values = [
                    float(self.table.item(row, column).text())
                    for column in range(len(_COLUMNS))
                ]
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError(f"第 {row + 1} 行必须是数字") from exc
            t, x, y, width, height, opacity = values
            if t < 0:
                raise ValueError(f"第 {row + 1} 行时间不能为负")
            if not (0 <= x <= 1 and 0 <= y <= 1):
                raise ValueError(f"第 {row + 1} 行原点必须在 0..1")
            if width <= 0 or height <= 0 or x + width > 1.0001 or y + height > 1.0001:
                raise ValueError(f"第 {row + 1} 行尺寸超出画布")
            if not (0 <= opacity <= 1):
                raise ValueError(f"第 {row + 1} 行透明度必须在 0..1")
            result.append(
                {
                    "t": RationalTime.from_seconds(
                        f"{t:.6f}", rate=90_000
                    ).model_dump(),
                    "x": x,
                    "y": y,
                    "width": width,
                    "height": height,
                    "opacity": opacity,
                }
            )
        result.sort(
            key=lambda item: RationalTime.model_validate(item["t"]).to_float()
        )
        times = [
            RationalTime.model_validate(item["t"]).to_float() for item in result
        ]
        if len(times) != len(set(times)):
            raise ValueError("关键帧时间不能重复")
        return result
