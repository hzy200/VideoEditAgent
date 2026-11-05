from __future__ import annotations

from typing import Any

import json

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from smartcut.desktop.formatting import seconds_or_none

MEDIA_MIME = "application/x-smartcut-media"


class MediaBinList(QListWidget):
    """素材 bin：支持把素材拖入时间线。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setDragDropMode(QListWidget.DragDropMode.DragOnly)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)

    def mimeData(self, items) -> QMimeData:  # type: ignore[override]
        data = QMimeData()
        payload = [
            item.data(Qt.ItemDataRole.UserRole) for item in items
        ]
        data.setData(
            MEDIA_MIME,
            json.dumps(
                [item for item in payload if item], ensure_ascii=False
            ).encode("utf-8"),
        )
        return data


class ProjectPanel(QWidget):
    new_project = Signal()
    open_project = Signal(str)
    rename_project = Signal(str)
    delete_project = Signal(str)
    import_media = Signal()
    add_media = Signal()
    add_pip = Signal(object)
    upload_media = Signal()
    transcribe = Signal()
    asset_selected = Signal(object)
    asset_activated = Signal(object)
    refresh = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(270)
        self.setMaximumWidth(380)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        project_group = QGroupBox("项目")
        project_layout = QVBoxLayout(project_group)
        self.project_list = QListWidget()
        self.project_list.currentItemChanged.connect(self._on_current_changed)
        project_layout.addWidget(self.project_list)

        row = QHBoxLayout()
        self.new_button = QPushButton("新建项目")
        self.refresh_button = QPushButton("刷新")
        row.addWidget(self.new_button)
        row.addWidget(self.refresh_button)
        project_layout.addLayout(row)

        manage_row = QHBoxLayout()
        self.rename_button = QPushButton("重命名")
        self.delete_button = QPushButton("删除")
        self.delete_button.setObjectName("dangerButton")
        manage_row.addWidget(self.rename_button)
        manage_row.addWidget(self.delete_button)
        project_layout.addLayout(manage_row)
        layout.addWidget(project_group, 1)

        media_group = QGroupBox("素材 bin")
        media_layout = QVBoxLayout(media_group)
        self.import_button = QPushButton("导入主素材")
        self.import_button.setObjectName("primaryButton")
        self.add_button = QPushButton("添加素材")
        self.pip_button = QPushButton("画中画")
        self.upload_button = QPushButton("上传")
        self.transcribe_button = QPushButton("本地转写")
        self.replace_button = QPushButton("替换素材")
        actions = QGridLayout()
        actions.setSpacing(6)
        actions.addWidget(self.import_button, 0, 0)
        actions.addWidget(self.add_button, 0, 1)
        actions.addWidget(self.pip_button, 1, 0)
        actions.addWidget(self.upload_button, 1, 1)
        actions.addWidget(self.transcribe_button, 2, 0)
        actions.addWidget(self.replace_button, 2, 1)
        media_layout.addLayout(actions)
        self.media_list = MediaBinList()
        self.media_list.setMinimumHeight(90)
        self.media_list.currentItemChanged.connect(self._on_asset_changed)
        self.media_list.itemDoubleClicked.connect(self._on_asset_activated)
        media_layout.addWidget(self.media_list)

        form = QFormLayout()
        self.name_label = QLabel("-")
        self.duration_label = QLabel("-")
        self.resolution_label = QLabel("-")
        self.fps_label = QLabel("-")
        self.audio_label = QLabel("-")
        self.src_label = QLabel("-")
        self.src_label.setWordWrap(True)
        self.src_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("素材名", self.name_label)
        form.addRow("时长", self.duration_label)
        form.addRow("分辨率", self.resolution_label)
        form.addRow("帧率", self.fps_label)
        form.addRow("音频", self.audio_label)
        form.addRow("工作区路径", self.src_label)
        media_layout.addLayout(form)
        layout.addWidget(media_group)

        self.new_button.clicked.connect(self.new_project.emit)
        self.refresh_button.clicked.connect(self.refresh.emit)
        self.rename_button.clicked.connect(self._emit_rename)
        self.delete_button.clicked.connect(self._emit_delete)
        self.import_button.clicked.connect(self.import_media.emit)
        self.add_button.clicked.connect(self.add_media.emit)
        self.pip_button.clicked.connect(self._emit_pip)
        self.upload_button.clicked.connect(self.upload_media.emit)
        self.transcribe_button.clicked.connect(self.transcribe.emit)
        self.replace_button.clicked.connect(self.import_media.emit)
        self.set_project_enabled(False)

    def set_project_enabled(self, enabled: bool) -> None:
        for button in (
            self.import_button,
            self.add_button,
            self.pip_button,
            self.upload_button,
            self.transcribe_button,
            self.replace_button,
            self.rename_button,
            self.delete_button,
        ):
            button.setEnabled(enabled)
        self.media_list.setEnabled(enabled)

    def set_projects(self, projects: list[dict]) -> None:
        current_id = self.selected_project_id()
        self.project_list.blockSignals(True)
        self.project_list.clear()
        for project in projects:
            item = QListWidgetItem(f"{project.get('name') or project['id']}")
            item.setData(Qt.ItemDataRole.UserRole, project["id"])
            item.setToolTip(project.get("workspace_dir", ""))
            self.project_list.addItem(item)
        if current_id:
            self.select_project(current_id)
        elif projects:
            self.project_list.setCurrentRow(0)
        if not projects:
            self.set_project_enabled(False)
        self.project_list.blockSignals(False)

    def select_project(self, project_id: str) -> None:
        self.project_list.blockSignals(True)
        try:
            for index in range(self.project_list.count()):
                item = self.project_list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == project_id:
                    self.project_list.setCurrentItem(item)
                    break
        finally:
            self.project_list.blockSignals(False)

    def selected_project_id(self) -> str | None:
        item = self.project_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def set_assets(self, assets: list[dict]) -> None:
        current = self.selected_asset()
        current_src = (current or {}).get("src")
        self.media_list.blockSignals(True)
        self.media_list.clear()
        for asset in assets or []:
            facts = asset.get("facts") or {}
            duration = seconds_or_none(facts.get("duration"))
            kind = asset.get("kind") or (
                "video" if facts.get("video") else "audio"
            )
            primary = "★ " if asset.get("is_primary") else ""
            label = (
                f"{primary}{asset.get('name') or asset.get('src')}"
                f" · {kind}"
                + (f" · {duration:.1f}s" if duration is not None else "")
            )
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, asset)
            item.setToolTip(str(asset.get("src") or ""))
            self.media_list.addItem(item)
        if current_src:
            for index in range(self.media_list.count()):
                item = self.media_list.item(index)
                data = item.data(Qt.ItemDataRole.UserRole) or {}
                if data.get("src") == current_src:
                    self.media_list.setCurrentItem(item)
                    break
        elif assets:
            self.media_list.setCurrentRow(0)
        self.media_list.blockSignals(False)

    def selected_asset(self) -> dict | None:
        item = self.media_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def set_project(self, project: dict) -> None:
        self.set_project_enabled(True)
        asset = project.get("asset")
        assets = project.get("assets") or ([asset] if asset else [])
        self.set_assets(assets)
        if not asset:
            self.name_label.setText("-")
            self.duration_label.setText("-")
            self.resolution_label.setText("-")
            self.fps_label.setText("-")
            self.audio_label.setText("-")
            self.src_label.setText(project.get("workspace_dir", "-"))
            return

        facts = asset.get("facts") or {}
        duration = seconds_or_none(facts.get("duration"))
        self.name_label.setText(str(asset.get("src", "-")))
        self.duration_label.setText(f"{duration:.3f}s" if duration is not None else "-")
        width = facts.get("width")
        height = facts.get("height")
        self.resolution_label.setText(f"{width}x{height}" if width and height else "-")
        self.fps_label.setText(str(facts.get("fps") or "-"))
        self.audio_label.setText("有" if facts.get("has_audio") else "无")
        self.src_label.setText(project.get("workspace_dir", "-"))

    def _emit_rename(self) -> None:
        project_id = self.selected_project_id()
        if project_id:
            self.rename_project.emit(project_id)

    def _emit_delete(self) -> None:
        project_id = self.selected_project_id()
        if project_id:
            self.delete_project.emit(project_id)

    def _on_current_changed(self, current: QListWidgetItem | None, _previous) -> None:
        if current is not None:
            self.open_project.emit(current.data(Qt.ItemDataRole.UserRole))

    def _on_asset_changed(self, current: QListWidgetItem | None, _previous) -> None:
        if current is not None:
            self.asset_selected.emit(current.data(Qt.ItemDataRole.UserRole) or {})

    def _on_asset_activated(self, item: QListWidgetItem) -> None:
        self.asset_activated.emit(item.data(Qt.ItemDataRole.UserRole) or {})

    def _emit_pip(self) -> None:
        asset = self.selected_asset()
        if asset:
            self.add_pip.emit(asset)
