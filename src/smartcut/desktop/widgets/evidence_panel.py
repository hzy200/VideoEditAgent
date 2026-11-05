"""证据面板：缩略图、tile 编号、时间码与点击定位（只读）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

KIND_LABELS = {
    "contact_sheet": "通用 contact sheet",
    "temporal_diff": "帧差/光流",
    "flow_strip": "光流条带",
    "bbox_overlay": "人脸/主体框",
    "roi_crop": "ROI 高分辨率",
    "codec_residual": "codec 残差",
}


class EvidencePanel(QWidget):
    refresh_requested = Signal(str)
    seek_requested = Signal(float)
    tile_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._workspace_root: Path | None = None
        self._payload: dict | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        controls = QHBoxLayout()
        self.kind_combo = QComboBox()
        for kind, label in KIND_LABELS.items():
            self.kind_combo.addItem(label, kind)
        self.refresh_button = QPushButton("生成/刷新证据")
        controls.addWidget(self.kind_combo, 1)
        controls.addWidget(self.refresh_button)
        layout.addLayout(controls)

        self.status = QLabel("尚未生成视觉证据")
        self.status.setWordWrap(True)
        self.status.setObjectName("mutedText")
        layout.addWidget(self.status)

        self.preview = QLabel("证据视图将在这里显示")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(150)
        self.preview.setObjectName("mediaCanvas")
        layout.addWidget(self.preview)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["标签", "时间(s)", "帧号", "PTS"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemSelectionChanged.connect(self._emit_seek)
        layout.addWidget(self.table, 1)

        self.refresh_button.clicked.connect(self._emit_refresh)

    def set_workspace_root(self, root: str | Path | None) -> None:
        self._workspace_root = Path(root) if root else None
        if self._payload is not None:
            self._load_preview()

    def set_evidence(self, payload: dict | None) -> None:
        self._payload = payload if (payload or {}).get("available") else None
        self.table.setRowCount(0)
        if self._payload is None:
            warning = (payload or {}).get("warning") or "暂无证据"
            self.status.setText(str(warning))
            self.preview.setText("证据视图将在这里显示")
            self.preview.setPixmap(QPixmap())
            return
        evidence = self._payload.get("evidence") or {}
        tiles = (evidence.get("tile_map") or {}).get("tiles") or []
        self.status.setText(
            f"{KIND_LABELS.get(self._payload.get('kind'), self._payload.get('kind'))}"
            f" · {len(tiles)} 个 tile · "
            f"{'缓存' if self._payload.get('cached') else '新生成'}"
            f" · backend={evidence.get('meta', {}).get('backend', '-')}"
        )
        self.table.setRowCount(len(tiles))
        for row, tile in enumerate(tiles):
            seconds = None
            try:
                seconds = float(tile["t"]["value"]) / float(tile["t"]["rate"])
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                pass
            values = [
                str(tile.get("label") or ""),
                f"{seconds:.3f}" if seconds is not None else "-",
                str(tile.get("frame_no") if tile.get("frame_no") is not None else "-"),
                str(tile.get("pts") if tile.get("pts") is not None else "-"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(
                    f"{tile.get('label')} · "
                    f"{seconds if seconds is not None else '-'}s"
                )
                self.table.setItem(row, column, item)
        self._load_preview()

    def clear(self) -> None:
        self._payload = None
        self.table.setRowCount(0)
        self.status.setText("尚未生成视觉证据")
        self.preview.setText("证据视图将在这里显示")
        self.preview.setPixmap(QPixmap())

    def _load_preview(self) -> None:
        if self._payload is None or self._workspace_root is None:
            return
        relative = self._payload.get("image_ref")
        if not relative:
            return
        try:
            root = self._workspace_root.resolve()
            path = (root / str(relative)).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                return
        except (OSError, ValueError):
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return
        self.preview.setPixmap(
            pixmap.scaledToHeight(
                max(120, self.preview.height()),
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def current_kind(self) -> str:
        return str(self.kind_combo.currentData() or "contact_sheet")

    def _emit_refresh(self) -> None:
        self.refresh_requested.emit(self.current_kind())

    def _emit_seek(self) -> None:
        if self._payload is None:
            return
        evidence = self._payload.get("evidence") or {}
        tiles = (evidence.get("tile_map") or {}).get("tiles") or []
        row = self.table.currentRow()
        if not (0 <= row < len(tiles)):
            return
        tile = tiles[row]
        try:
            seconds = float(tile["t"]["value"]) / float(tile["t"]["rate"])
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return
        self.seek_requested.emit(seconds)
        self.tile_selected.emit(tile)
