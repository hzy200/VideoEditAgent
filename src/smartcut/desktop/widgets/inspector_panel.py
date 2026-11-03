from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


class InspectorPanel(QWidget):
    revision_selected = Signal(int)
    refresh_requested = Signal()
    save_requested = Signal()
    dry_run_requested = Signal(int)
    render_requested = Signal(int)
    diff_requested = Signal(int, int)
    open_output_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        revision_group = QGroupBox("版本")
        revision_layout = QVBoxLayout(revision_group)
        row = QHBoxLayout()
        self.revision_combo = QComboBox()
        self.refresh_button = QPushButton("刷新")
        row.addWidget(self.revision_combo, 1)
        row.addWidget(self.refresh_button)
        revision_layout.addLayout(row)

        diff_row = QHBoxLayout()
        self.base_combo = QComboBox()
        self.diff_button = QPushButton("比较版本")
        diff_row.addWidget(QLabel("对比"))
        diff_row.addWidget(self.base_combo, 1)
        diff_row.addWidget(self.diff_button)
        revision_layout.addLayout(diff_row)
        self.diff_browser = QTextBrowser()
        self.diff_browser.setMinimumHeight(70)
        self.diff_browser.setPlainText("选择两个版本查看结构差异")
        revision_layout.addWidget(self.diff_browser)
        layout.addWidget(revision_group)

        action_group = QGroupBox("编辑与导出")
        action_layout = QVBoxLayout(action_group)
        self.save_button = QPushButton("保存为新版本")
        self.dry_run_button = QPushButton("Dry-run 查看工具调用")
        self.render_button = QPushButton("渲染成片")
        self.open_output_button = QPushButton("打开产物目录")
        for button in (
            self.save_button,
            self.dry_run_button,
            self.render_button,
            self.open_output_button,
        ):
            action_layout.addWidget(button)
        layout.addWidget(action_group)

        artifact_group = QGroupBox("产物")
        artifact_layout = QVBoxLayout(artifact_group)
        self.workspace_label = QLabel("-")
        self.workspace_label.setWordWrap(True)
        self.workspace_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        artifact_layout.addWidget(QLabel("项目工作区"))
        artifact_layout.addWidget(self.workspace_label)
        self.artifact_list = QListWidget()
        artifact_layout.addWidget(self.artifact_list)
        layout.addWidget(artifact_group, 1)

        self.revision_combo.currentIndexChanged.connect(self._emit_revision)
        self.diff_button.clicked.connect(self._emit_diff)
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.save_button.clicked.connect(self.save_requested.emit)
        self.dry_run_button.clicked.connect(self._emit_dry_run)
        self.render_button.clicked.connect(self._emit_render)
        self.open_output_button.clicked.connect(self.open_output_requested.emit)
        self.set_revisions([])

    def set_revisions(self, revisions: list[dict]) -> None:
        current = self.current_revision()
        self.revision_combo.blockSignals(True)
        self.base_combo.blockSignals(True)
        self.revision_combo.clear()
        self.base_combo.clear()
        for revision in revisions:
            number = int(revision["revision"])
            self.revision_combo.addItem(f"revision {number}", number)
            self.base_combo.addItem(f"revision {number}", number)
        if current is not None:
            index = self.revision_combo.findData(current)
            if index >= 0:
                self.revision_combo.setCurrentIndex(index)
        self.revision_combo.blockSignals(False)
        self.base_combo.blockSignals(False)
        has_revision = bool(revisions)
        for button in (self.save_button, self.dry_run_button, self.render_button):
            button.setEnabled(has_revision)
        self.diff_button.setEnabled(len(revisions) >= 2)

    def select_revision(self, revision_no: int) -> None:
        self.revision_combo.blockSignals(True)
        try:
            for index in range(self.revision_combo.count()):
                if self.revision_combo.itemData(index) == revision_no:
                    self.revision_combo.setCurrentIndex(index)
                    break
        finally:
            self.revision_combo.blockSignals(False)
        previous = revision_no - 1
        base_index = self.base_combo.findData(previous)
        if base_index >= 0:
            self.base_combo.setCurrentIndex(base_index)

    def current_revision(self) -> int | None:
        data = self.revision_combo.currentData()
        return int(data) if data is not None else None

    def set_workspace(self, path: str) -> None:
        self.workspace_label.setText(path)

    def set_artifacts(self, artifacts: list[str]) -> None:
        self.artifact_list.clear()
        self.artifact_list.addItems(artifacts)

    def set_busy(self, busy: bool) -> None:
        for button in (
            self.save_button,
            self.dry_run_button,
            self.render_button,
            self.refresh_button,
        ):
            button.setEnabled(not busy and (button is not self.save_button or self.current_revision() is not None))

    def _emit_revision(self) -> None:
        revision = self.current_revision()
        if revision is not None:
            self.revision_selected.emit(revision)

    def _emit_dry_run(self) -> None:
        revision = self.current_revision()
        if revision is not None:
            self.dry_run_requested.emit(revision)

    def _emit_render(self) -> None:
        revision = self.current_revision()
        if revision is not None:
            self.render_requested.emit(revision)

    def _emit_diff(self) -> None:
        revision = self.current_revision()
        base = self.base_combo.currentData()
        if revision is None or base is None or int(base) == revision:
            self.diff_browser.setPlainText("请选择两个不同版本进行对比")
            return
        self.diff_requested.emit(revision, int(base))

    def show_diff(self, payload: dict | None) -> None:
        if not payload:
            self.diff_browser.setPlainText("暂无差异")
            return
        lines = [
            f"revision {payload.get('base_revision')} → {payload.get('revision')}",
            f"新增 {payload.get('added_count', 0)} · "
            f"删除 {payload.get('removed_count', 0)} · "
            f"未变 {payload.get('unchanged_count', 0)}",
            "",
        ]
        for item in payload.get("added") or []:
            lines.append(
                f"+ {item.get('start_seconds')}–{item.get('end_seconds')}s "
                f"{item.get('tag') or ''}"
            )
        for item in payload.get("removed") or []:
            lines.append(
                f"- {item.get('start_seconds')}–{item.get('end_seconds')}s "
                f"{item.get('tag') or ''}"
            )
        for item in payload.get("unchanged") or []:
            lines.append(
                f"= {item.get('start_seconds')}–{item.get('end_seconds')}s "
                f"{item.get('tag') or ''}"
            )
        self.diff_browser.setPlainText("\n".join(lines))
