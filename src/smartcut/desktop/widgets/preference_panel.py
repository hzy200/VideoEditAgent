"""偏好面板：展示用户编辑统计，并切换偏好作用域。"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)


class PreferencePanel(QWidget):
    scope_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.summary = QLabel("样本不足：至少保存 2 次手动剪辑后开始统计")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.tags = QLabel("常用标签：-")
        self.tags.setWordWrap(True)
        self.tags.setStyleSheet("color:#64748b;")
        layout.addWidget(self.tags)

        self.notes = QLabel("")
        self.notes.setWordWrap(True)
        self.notes.setStyleSheet("color:#64748b;")
        layout.addWidget(self.notes)

        self.pace_checkbox = QCheckBox("按我的节奏规划（跨素材复用偏好）")
        self.pace_checkbox.setToolTip(
            "关闭时只使用当前素材的历史编辑统计；开启后在项目内跨素材复用。"
        )
        layout.addWidget(self.pace_checkbox)
        layout.addStretch(1)

        self.pace_checkbox.toggled.connect(self._on_toggled)

    def set_preference(self, payload: dict | None) -> None:
        preference = (payload or {})
        stats = preference.get("stats") or {}
        sample_size = int(stats.get("sample_size") or 0)
        if sample_size < 2:
            self.summary.setText(
                "样本不足：至少保存 2 次手动剪辑后开始统计"
            )
            self.tags.setText("常用标签：-")
            self.notes.setText("")
            return
        self.summary.setText(
            f"样本数：{sample_size} · 片段中位："
            f"{float(stats.get('median_segment_seconds') or 0):.1f}s · "
            f"保留率中位：{float(stats.get('keep_ratio') or 0):.0%}"
        )
        top_tags = stats.get("top_tags") or []
        self.tags.setText(
            "常用标签：" + ("、".join(str(tag) for tag in top_tags) or "-")
        )
        self.notes.setText("；".join(str(note) for note in preference.get("notes") or []))

    def set_scope(self, scope: str) -> None:
        self.pace_checkbox.blockSignals(True)
        self.pace_checkbox.setChecked(scope == "project")
        self.pace_checkbox.blockSignals(False)

    def scope(self) -> str:
        return "project" if self.pace_checkbox.isChecked() else "source"

    def _on_toggled(self, checked: bool) -> None:
        self.scope_changed.emit("project" if checked else "source")
