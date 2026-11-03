from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QEvent, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from pydantic import TypeAdapter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QSplitter,
    QStyle,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.effects import Effect
from smartcut.contracts.time import RationalTime, fps_from_string
from smartcut.desktop.controller import DesktopController
from smartcut.desktop.edit_session import EditSession, EditSnapshot
from smartcut.desktop.formatting import seconds_or_none
from smartcut.desktop.paths import bundled_separator
from smartcut.desktop.settings_store import DesktopConfig, SettingsStore
from smartcut.desktop.theme import apply_theme
from smartcut.desktop.widgets.ai_panel import AIPanel
from smartcut.desktop.widgets.audio_panel import AudioPanel
from smartcut.desktop.widgets.candidate_panel import CandidatePanel
from smartcut.desktop.widgets.diagnostics_panel import DiagnosticsPanel
from smartcut.desktop.widgets.effects_panel import EffectsPanel
from smartcut.desktop.widgets.export_dialog import ExportDialog
from smartcut.desktop.widgets.inspector_panel import InspectorPanel
from smartcut.desktop.widgets.metrics_panel import MetricsPanel
from smartcut.desktop.widgets.outline_panel import OutlinePanel
from smartcut.desktop.widgets.perception_panel import PerceptionPanel
from smartcut.desktop.widgets.preview_panel import PreviewPanel
from smartcut.desktop.widgets.project_panel import ProjectPanel
from smartcut.desktop.widgets.queue_panel import QueuePanel
from smartcut.desktop.widgets.runs_panel import RunsPanel
from smartcut.desktop.widgets.settings_dialog import SettingsDialog
from smartcut.desktop.widgets.subtitle_panel import SubtitlePanel
from smartcut.desktop.widgets.timeline_panel import TimelinePanel
from smartcut.desktop.widgets.timing_panel import TimingPanel
from smartcut.logging import get_logger

_LOG = get_logger("smartcut.desktop.main_window")
_EFFECT_LIST = TypeAdapter(list[Effect])
_HEADLESS_PLATFORMS = {"offscreen", "minimal"}


def _headless_ui() -> bool:
    """离屏/无头环境下没人能点模态框，exec 会永久阻塞调用线程。"""
    if os.environ.get("SMARTCUT_HEADLESS") == "1":
        return True
    app = QApplication.instance()
    platform = (
        app.platformName()
        if app is not None
        else os.environ.get("QT_QPA_PLATFORM", "")
    )
    return str(platform).strip().lower() in _HEADLESS_PLATFORMS


class MainWindow(QMainWindow):
    def __init__(
        self,
        controller: DesktopController,
        settings: Settings,
        store: SettingsStore,
        data_dir: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.settings = settings
        self.store = store
        self.data_dir = Path(data_dir)
        self.config: DesktopConfig = store.load()
        self.current_project: dict | None = None
        self.current_ir: dict | None = None
        self.artifacts: list[str] = []
        self._loaded_project_id: str | None = None
        self._showing_output = False
        self.unsaved = False
        self._pending_after_save: Any | None = None
        self._close_after_save = False
        self._subtitle_cues: list[dict] = []
        self._subtitles_dirty = False
        self._session = EditSession()
        self._restoring = False
        self._timing = {
            "speeds": {},
            "transitions": [],
            "overlays": [],
            "removals": [],
        }
        self._overlay_assets: list[dict] = []
        self._preview_mix_timer = QTimer(self)
        self._preview_mix_timer.setSingleShot(True)
        self._preview_mix_timer.setInterval(600)
        self._preview_mix_timer.timeout.connect(self._request_preview_mix)
        self._effects_preview_timer = QTimer(self)
        self._effects_preview_timer.setSingleShot(True)
        self._effects_preview_timer.setInterval(600)
        self._effects_preview_timer.timeout.connect(self._request_effects_preview)
        self._error_box: QMessageBox | None = None
        self._resume_prompt_box: QMessageBox | None = None
        self._json_dialog: QDialog | None = None

        self.setWindowTitle("SmartCut — 智能剪辑")
        self.resize(1440, 900)
        self._build_ui()
        self._build_toolbar()
        self.preview_panel.set_caption_style(
            font_size=settings.caption_font_size,
            color=settings.caption_color,
            position=settings.caption_position,
        )
        self.preview_panel.set_volume(self.config.preview_volume, notify=False)
        self.preview_panel.set_muted(self.config.preview_muted)
        self._build_menu()
        self._connect()
        app = QApplication.instance()
        if app is not None:
            apply_theme(
                app,
                self.config.theme,
                scale=self.config.ui_scale,
                contrast=self.config.high_contrast,
            )
            app.installEventFilter(self)
        self._restore_window_state()
        self.controller.refresh_projects()

    # ── UI ────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter = splitter

        self.project_panel = ProjectPanel()
        splitter.addWidget(self.project_panel)

        center_splitter = QSplitter(Qt.Orientation.Vertical)
        self.center_splitter = center_splitter
        self.preview_panel = PreviewPanel()
        self.timeline_panel = TimelinePanel()
        center_splitter.addWidget(self.preview_panel)
        center_splitter.addWidget(self.timeline_panel)
        center_splitter.setSizes([560, 280])
        splitter.addWidget(center_splitter)

        self.tabs = QTabWidget()
        self.ai_panel = AIPanel()
        self.inspector_panel = InspectorPanel()
        self.subtitle_panel = SubtitlePanel()
        self.audio_panel = AudioPanel()
        self.effects_panel = EffectsPanel()
        self.timing_panel = TimingPanel()
        self.outline_panel = OutlinePanel()
        self.perception_panel = PerceptionPanel()
        self.candidate_panel = CandidatePanel()
        self.metrics_panel = MetricsPanel()
        self.runs_panel = RunsPanel()
        self.queue_panel = QueuePanel()
        self.diagnostics_panel = DiagnosticsPanel()
        self.tabs.addTab(self.ai_panel, "智能剪辑")
        self.tabs.addTab(self.inspector_panel, "版本与片段")
        self.tabs.addTab(self.subtitle_panel, "字幕")
        self.tabs.addTab(self.audio_panel, "音频")
        self.tabs.addTab(self.effects_panel, "特效")
        self.tabs.addTab(self.timing_panel, "转场/变速/水印")
        self.tabs.addTab(self.outline_panel, "大纲确认")
        self.tabs.addTab(self.perception_panel, "感知")
        self.tabs.addTab(self.candidate_panel, "候选片段")
        self.tabs.addTab(self.metrics_panel, "运行指标")
        self.tabs.addTab(self.runs_panel, "运行记录")
        self.tabs.addTab(self.queue_panel, "任务队列")
        self.tabs.addTab(self.diagnostics_panel, "诊断")
        splitter.addWidget(self.tabs)

        splitter.setSizes([300, 760, 380])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        layout.addWidget(splitter)
        self.setCentralWidget(central)

        self.status_label = QLabel("就绪")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setMaximumWidth(220)
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.progress)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("主工具栏", self)
        toolbar.setObjectName("mainToolbar")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)
        self.toolbar = toolbar

        def icon(pixmap: QStyle.StandardPixmap):
            return self.style().standardIcon(pixmap)

        new_action = toolbar.addAction(icon(QStyle.StandardPixmap.SP_FileIcon), "新建")
        new_action.triggered.connect(self._on_new_project)
        import_action = toolbar.addAction(
            icon(QStyle.StandardPixmap.SP_DialogOpenButton), "导入"
        )
        import_action.triggered.connect(self._on_import)
        upload_action = toolbar.addAction(
            icon(QStyle.StandardPixmap.SP_ArrowUp), "上传"
        )
        upload_action.triggered.connect(self._on_upload)
        toolbar.addSeparator()
        undo_action = toolbar.addAction(
            icon(QStyle.StandardPixmap.SP_ArrowBack), "撤销"
        )
        undo_action.triggered.connect(self._undo)
        redo_action = toolbar.addAction(
            icon(QStyle.StandardPixmap.SP_ArrowForward), "重做"
        )
        redo_action.triggered.connect(self._redo)
        toolbar.addSeparator()
        self.toolbar_play_action = toolbar.addAction(
            icon(QStyle.StandardPixmap.SP_MediaPlay), "播放"
        )
        self.toolbar_play_action.setToolTip("播放 / 暂停（空格）")
        self.toolbar_play_action.triggered.connect(self.preview_panel.toggle_play)
        stop_action = toolbar.addAction(
            icon(QStyle.StandardPixmap.SP_MediaStop), "停止"
        )
        stop_action.triggered.connect(self.preview_panel.stop)
        toolbar.addSeparator()
        save_action = toolbar.addAction(
            icon(QStyle.StandardPixmap.SP_DialogSaveButton), "保存版本"
        )
        save_action.triggered.connect(self._on_save_revision)
        self.toolbar_render_action = toolbar.addAction(
            icon(QStyle.StandardPixmap.SP_DialogApplyButton), "渲染"
        )
        self.toolbar_render_action.triggered.connect(self._on_toolbar_render)
        self.preview_panel.playing_changed.connect(self._on_playing_changed)

    def _on_playing_changed(self, playing: bool) -> None:
        icon = (
            QStyle.StandardPixmap.SP_MediaPause
            if playing
            else QStyle.StandardPixmap.SP_MediaPlay
        )
        self.toolbar_play_action.setIcon(self.style().standardIcon(icon))
        self.toolbar_play_action.setText("暂停" if playing else "播放")

    def _on_toolbar_render(self) -> None:
        revision = self.inspector_panel.current_revision()
        if revision is not None:
            self._on_render(revision)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件")

        new_action = QAction("新建项目", self)
        new_action.setShortcut("Ctrl+N")
        new_action.triggered.connect(self._on_new_project)
        file_menu.addAction(new_action)

        import_action = QAction("导入素材", self)
        import_action.setShortcut("Ctrl+O")
        import_action.triggered.connect(self._on_import)
        file_menu.addAction(import_action)

        upload_action = QAction("上传素材", self)
        upload_action.setShortcut("Ctrl+Shift+O")
        upload_action.triggered.connect(self._on_upload)
        file_menu.addAction(upload_action)

        file_menu.addSeparator()
        export_action = QAction("导出 / 转码…", self)
        export_action.setShortcut("Ctrl+E")
        export_action.triggered.connect(self._on_export)
        file_menu.addAction(export_action)

        file_menu.addSeparator()
        settings_action = QAction("设置", self)
        settings_action.triggered.connect(self._on_settings)
        file_menu.addAction(settings_action)

        file_menu.addSeparator()
        exit_action = QAction("退出", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        project_menu = self.menuBar().addMenu("项目")
        refresh_action = QAction("刷新项目", self)
        refresh_action.triggered.connect(self._on_refresh_project)
        project_menu.addAction(refresh_action)

        transcribe_action = QAction("本地转写", self)
        transcribe_action.triggered.connect(self._on_transcribe)
        project_menu.addAction(transcribe_action)

        output_action = QAction("打开产物目录", self)
        output_action.triggered.connect(self._on_open_output)
        project_menu.addAction(output_action)

        help_menu = self.menuBar().addMenu("帮助")
        about_action = QAction("关于 SmartCut", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

    def _connect(self) -> None:
        self.project_panel.new_project.connect(self._on_new_project)
        self.project_panel.open_project.connect(self._on_project_selected)
        self.project_panel.import_media.connect(self._on_import)
        self.project_panel.upload_media.connect(self._on_upload)
        self.project_panel.transcribe.connect(self._on_transcribe)
        self.project_panel.refresh.connect(self._on_refresh_project)
        self.project_panel.rename_project.connect(self._on_rename_project)
        self.project_panel.delete_project.connect(self._on_delete_project)

        self.ai_panel.run_requested.connect(self._on_orchestrate)
        self.ai_panel.cancel_requested.connect(self._on_cancel_jobs)
        self.ai_panel.review_requested.connect(self._on_review_render)
        self.outline_panel.approve_requested.connect(self._on_outline_approve)
        self.outline_panel.refresh_requested.connect(self._on_outline_refresh)
        self.outline_panel.seek_requested.connect(self.preview_panel.seek)
        self.runs_panel.resume_requested.connect(self._on_resume_run)
        self.runs_panel.run_selected.connect(self.controller.load_run)
        self.queue_panel.cancel_requested.connect(self._on_cancel_jobs_by_id)
        self.queue_panel.refresh_requested.connect(self.controller.refresh_jobs)
        self.diagnostics_panel.refresh_requested.connect(self.controller.load_diagnostics)
        self.diagnostics_panel.open_log_requested.connect(self._on_open_log_dir)
        self.inspector_panel.refresh_requested.connect(self._on_refresh_project)
        self.inspector_panel.save_requested.connect(self._on_save_revision)
        self.inspector_panel.dry_run_requested.connect(self._on_dry_run)
        self.inspector_panel.render_requested.connect(self._on_render)
        self.inspector_panel.open_output_requested.connect(self._on_open_output)
        self.subtitle_panel.cues_changed.connect(self._on_subtitles_changed)
        self.subtitle_panel.add_requested.connect(self._on_subtitle_add)
        self.subtitle_panel.reload_requested.connect(self._on_subtitle_reload)
        self.subtitle_panel.burn_changed.connect(self._on_burn_changed)
        self.audio_panel.audio_changed.connect(self._on_audio_changed)
        self.audio_panel.tracks_changed.connect(self._on_audio_tracks_changed)
        self.audio_panel.import_requested.connect(self._on_import_audio)
        self.audio_panel.separate_requested.connect(self._on_separate_audio)
        self.effects_panel.effects_changed.connect(self._on_effects_changed)
        self.effects_panel.preview_requested.connect(self._request_effects_preview)
        self.timing_panel.changed.connect(self._on_timing_changed)
        self.timing_panel.overlay_import_requested.connect(
            self._on_import_overlay_asset
        )
        self.timeline_panel.undo_requested.connect(self._undo)
        self.timeline_panel.redo_requested.connect(self._redo)

        self.timeline_panel.seek_requested.connect(self.preview_panel.seek)
        self.timeline_panel.segment_selected.connect(self._on_segment_selected)
        self.timeline_panel.selection_cleared.connect(self.preview_panel.clear_loop)
        self.timeline_panel.modified.connect(self._on_timeline_modified)
        self.perception_panel.keyframe_selected.connect(self.preview_panel.seek)
        self.preview_panel.position_changed.connect(self.timeline_panel.set_position)
        self.preview_panel.position_changed.connect(self.audio_panel.set_playhead)
        self.candidate_panel.search_requested.connect(self.controller.search_candidates)
        self.candidate_panel.similar_requested.connect(
            self.controller.find_similar_candidates
        )
        self.candidate_panel.add_requested.connect(self.timeline_panel.add_candidates)
        self.inspector_panel.diff_requested.connect(self.controller.diff_revisions)

        self.controller.projects_changed.connect(self._on_projects)
        self.controller.project_opened.connect(self._on_project_opened)
        self.controller.resumable_runs_found.connect(self._on_resumable_runs)
        self.controller.asset_changed.connect(self._on_asset_changed)
        self.controller.proxy_ready.connect(self._on_proxy_ready)
        self.controller.job_progress.connect(self._on_job_progress)
        self.controller.job_finished.connect(self._on_job_finished)
        self.controller.run_finished.connect(self._on_run_finished)
        self.controller.revisions_changed.connect(self._on_revisions)
        self.controller.revision_loaded.connect(self._on_revision_loaded)
        self.controller.runs_changed.connect(self.runs_panel.set_runs)
        self.controller.artifacts_changed.connect(self._on_artifacts)
        self.controller.perception_changed.connect(self._on_perception_changed)
        self.controller.outline_ready.connect(self._on_outline_ready)
        self.controller.outline_approved.connect(self._on_outline_approved)
        self.controller.render_reviewed.connect(self._on_render_reviewed)
        self.controller.candidates_changed.connect(self.candidate_panel.set_candidates)
        self.controller.diff_changed.connect(self._on_diff_changed)
        self.controller.waveform_changed.connect(self.timeline_panel.set_waveform)
        self.controller.captions_changed.connect(self._on_captions_changed)
        self.controller.jobs_changed.connect(self.queue_panel.set_jobs)
        self.controller.diagnostics_changed.connect(self.diagnostics_panel.set_diagnostics)
        self.controller.audio_assets_changed.connect(self._on_audio_assets_changed)
        self.controller.audio_waveforms_changed.connect(
            self.timeline_panel.set_audio_waveforms
        )
        self.controller.preview_mix_ready.connect(self._on_preview_mix_ready)
        self.controller.effects_preview_ready.connect(self._on_effects_preview_ready)
        self.controller.overlay_assets_changed.connect(
            self._on_overlay_assets_changed
        )
        self.controller.operation_done.connect(self._on_operation_done)
        self.controller.error.connect(self._on_error)

    # ── 窗口状态 ──────────────────────────────────────────────────────
    def _restore_window_state(self) -> None:
        if self.config.window_geometry:
            try:
                self.restoreGeometry(
                    QByteArray.fromBase64(self.config.window_geometry.encode("ascii"))
                )
            except Exception as exc:
                _LOG.debug("window_geometry_restore_failed", error=str(exc))
        if self.config.window_state:
            try:
                self.restoreState(
                    QByteArray.fromBase64(self.config.window_state.encode("ascii"))
                )
            except Exception as exc:
                _LOG.debug("window_state_restore_failed", error=str(exc))
        if self.config.splitter_sizes:
            self.main_splitter.setSizes(self.config.splitter_sizes)
        if self.config.center_splitter_sizes:
            self.center_splitter.setSizes(self.config.center_splitter_sizes)
        if 0 <= self.config.tab_index < self.tabs.count():
            self.tabs.setCurrentIndex(self.config.tab_index)

    def _capture_window_state(self) -> None:
        self.config.window_geometry = self.saveGeometry().toBase64().data().decode(
            "ascii"
        )
        self.config.window_state = self.saveState().toBase64().data().decode("ascii")
        self.config.splitter_sizes = list(self.main_splitter.sizes())
        self.config.center_splitter_sizes = list(self.center_splitter.sizes())
        self.config.tab_index = self.tabs.currentIndex()
        self.config.preview_volume = self.preview_panel.volume()
        self.config.preview_muted = self.preview_panel.is_muted()
        if self.controller.current_project:
            self.config.last_project_id = self.controller.current_project

    # ── 项目 ──────────────────────────────────────────────────────────
    def _on_projects(self, projects: list[dict]) -> None:
        self.project_panel.set_projects(projects)
        if not projects:
            self.current_project = None
            self._loaded_project_id = None
            self.timeline_panel.clear()
            self.preview_panel.clear()
            self.ai_panel.set_project_enabled(False)
            self.project_panel.set_project_enabled(False)
            self.subtitle_panel.set_cues([], burn=False)
            self.audio_panel.set_audio(None)
            self.audio_panel.set_assets([])
            self.audio_panel.set_context(main_duration=0)
            self.audio_panel.set_tracks([], main_duration=0)
            self.timeline_panel.set_audio_tracks([])
            self._subtitle_cues = []
            self._subtitles_dirty = False
            return

        ids = {project["id"] for project in projects}
        if self.controller.current_project in ids:
            self.project_panel.select_project(self.controller.current_project)
            return
        target = (
            self.config.last_project_id
            if self.config.last_project_id in ids
            else projects[0]["id"]
        )
        self.project_panel.select_project(target)
        self.controller.open_project(target)

    def _resolve_unsaved(self, action: str = "继续") -> str:
        if not self.unsaved:
            return "discard"
        if _headless_ui():
            _LOG.info("unsaved_prompt_suppressed", action=action)
            self.unsaved = False
            return "discard"
        answer = QMessageBox.question(
            self,
            "未保存的编辑修改",
            f"当前时间线/特效/音频有未保存修改。{action}前要保存为新版本吗？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            return "save"
        if answer == QMessageBox.StandardButton.Discard:
            self.unsaved = False
            return "discard"
        return "cancel"

    def _on_timeline_modified(self, changed: bool) -> None:
        if not changed:
            return
        self._sync_timing_panel()
        self._record_change()
        self._sync_effect_scope()
        self._set_status("时间线已修改（未保存）")

    # ── 统一编辑会话 ──────────────────────────────────────────────────
    def _capture_snapshot(self) -> EditSnapshot:
        ir = self.current_ir or {}
        return EditSnapshot(
            sequence=deepcopy(self.timeline_panel.segments()),
            effects=deepcopy(self.effects_panel.effects()),
            speeds=deepcopy(self._timing.get("speeds") or {}),
            transitions=deepcopy(self._timing.get("transitions") or []),
            overlays=deepcopy(self._timing.get("overlays") or []),
            removals=deepcopy(self._timing.get("removals") or []),
            audio=deepcopy(ir.get("audio") or {}),
            subtitles=(
                deepcopy(self._subtitle_cues)
                if self._subtitles_dirty
                else []
            ),
            subtitles_dirty=bool(self._subtitles_dirty),
            export=deepcopy(ir.get("export") or {}),
        )

    def _record_change(self) -> None:
        if self._restoring or self.current_ir is None:
            return
        self._session.capture(self._capture_snapshot())
        self.unsaved = self._session.dirty

    def _restore_snapshot(self, snapshot: EditSnapshot) -> None:
        self._restoring = True
        try:
            self.timeline_panel.restore_segments(deepcopy(snapshot.sequence))
            self.effects_panel.set_effects(deepcopy(snapshot.effects))
            self._timing = {
                "speeds": deepcopy(snapshot.speeds),
                "transitions": deepcopy(snapshot.transitions),
                "overlays": deepcopy(snapshot.overlays),
                "removals": deepcopy(snapshot.removals),
            }
            self._sync_timing_panel()
            self._sync_effect_scope()
            ir = dict(self.current_ir or {})
            ir["audio"] = deepcopy(snapshot.audio)
            ir["export"] = deepcopy(snapshot.export)
            self.current_ir = ir
            self.audio_panel.set_audio(snapshot.audio)
            self.audio_panel.set_tracks(
                snapshot.audio.get("tracks"),
                main_duration=(
                    self._main_duration(self.current_project)
                    if self.current_project
                    else 0.0
                ),
            )
            self.timeline_panel.set_audio_tracks(self.audio_panel.tracks())
            self._subtitle_cues = deepcopy(snapshot.subtitles)
            self._subtitles_dirty = bool(snapshot.subtitles_dirty)
            self.subtitle_panel.set_cues(
                self._subtitle_cues if snapshot.subtitles_dirty else [],
                burn=bool(snapshot.export.get("burn_captions")),
            )
            if not snapshot.subtitles_dirty:
                revision = int((self.current_ir or {}).get("revision") or 0)
                if revision > 0:
                    self.controller.load_captions(revision)
        finally:
            self._restoring = False

    def _undo(self) -> None:
        snapshot = self._session.undo()
        if snapshot is None:
            return
        self._restore_snapshot(snapshot)
        self.unsaved = self._session.dirty
        self._set_status("已撤销")

    def _redo(self) -> None:
        snapshot = self._session.redo()
        if snapshot is None:
            return
        self._restore_snapshot(snapshot)
        self.unsaved = self._session.dirty
        self._set_status("已重做")

    def _on_project_selected(self, project_id: str) -> None:
        if not project_id or project_id == self.controller.current_project:
            return
        decision = self._resolve_unsaved("切换项目")
        if decision == "cancel":
            self.project_panel.select_project(self.controller.current_project or "")
            return
        if decision == "save":
            current = self.controller.current_project
            self._pending_after_save = lambda: self.controller.open_project(project_id)
            self._on_save_revision()
            self.project_panel.select_project(current or "")
            return
        self.controller.open_project(project_id)

    def _on_rename_project(self, project_id: str) -> None:
        current = self.current_project if self.current_project and self.current_project.get("id") == project_id else None
        name, ok = QInputDialog.getText(
            self,
            "重命名项目",
            "项目名称：",
            text=(current or {}).get("name") or "",
        )
        if ok and name.strip():
            self.controller.rename_project(project_id, name.strip())

    def _on_delete_project(self, project_id: str) -> None:
        current = self.current_project if self.current_project and self.current_project.get("id") == project_id else None
        label = (current or {}).get("name") or project_id
        answer = QMessageBox.warning(
            self,
            "删除项目",
            f"确定删除项目“{label}”吗？\n工作区文件、版本、产物与缓存都会被删除，且不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.unsaved = False
        if self.config.last_project_id == project_id:
            self.config.last_project_id = None
        self.controller.delete_project(project_id)

    def _on_cancel_jobs_by_id(self, job_ids: list[str]) -> None:
        self.controller.cancel_jobs(list(job_ids or []))
        self._set_status("正在请求取消任务…")

    def _on_open_log_dir(self) -> None:
        folder = Path(self.settings.log_file).parent
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _on_resumable_runs(self, runs: list[dict]) -> None:
        if not runs:
            return
        if _headless_ui():
            _LOG.info("resume_prompt_suppressed", runs=len(runs))
            return
        if self._resume_prompt_box is not None:
            return
        latest = runs[0]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("恢复中断运行")
        box.setText(
            f"发现 {len(runs)} 个被进程中断的运行。\n"
            f"是否续跑最近一个：{str(latest.get('goal') or '')[:60]}？"
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        box.setModal(False)

        def resume() -> None:
            self._set_status("正在提交续跑…")
            self.controller.resume_run(str(latest["id"]))
            box.close()

        yes = box.button(QMessageBox.StandardButton.Yes)
        no = box.button(QMessageBox.StandardButton.No)
        if yes is not None:
            yes.clicked.connect(resume)
        if no is not None:
            no.clicked.connect(box.close)
        box.finished.connect(lambda _=0: self._clear_resume_prompt())
        box.show()
        self._resume_prompt_box = box

    def _clear_resume_prompt(self) -> None:
        box = self._resume_prompt_box
        self._resume_prompt_box = None
        if box is not None:
            box.deleteLater()

    def _on_project_opened(self, project: dict) -> None:
        project_id = project["id"]
        if self._loaded_project_id != project_id:
            self._loaded_project_id = project_id
            self._showing_output = False
            self.unsaved = False
            self.config.last_project_id = project_id
            self.timeline_panel.clear()
            self.preview_panel.clear()
            self.ai_panel.clear()
            self.outline_panel.clear()
            self.candidate_panel.clear()
            self.metrics_panel.clear()
            self.subtitle_panel.set_cues([])
            self.audio_panel.set_audio(None)
            self.effects_panel.clear()
            self._sync_effect_scope()
            self._timing = {
                "speeds": {},
                "transitions": [],
                "overlays": [],
                "removals": [],
            }
            self._overlay_assets = []
            self.timing_panel.set_overlay_assets([])
            self.timing_panel.clear()
            self._session.reset(None)
            self.audio_panel.set_assets(project.get("audio_assets") or [])
            self.audio_panel.set_context(main_duration=self._main_duration(project))
            self.audio_panel.set_tracks(None, main_duration=self._main_duration(project))
            self.timeline_panel.set_audio_tracks(self.audio_panel.tracks())
            self._subtitle_cues = []
            self._subtitles_dirty = False
            self.current_ir = None

        self.current_project = project
        self._overlay_assets = list(project.get("overlay_assets") or [])
        self.timing_panel.set_overlay_assets(self._overlay_assets)
        workspace_dir = project.get("workspace_dir")
        self.candidate_panel.set_workspace_root(
            Path(workspace_dir) if workspace_dir else None
        )
        self._on_audio_assets_changed(project.get("audio_assets") or [])
        self.project_panel.set_project(project)
        self.project_panel.select_project(project_id)
        self.inspector_panel.set_workspace(project.get("workspace_dir", "-"))
        self.ai_panel.set_project_enabled(project.get("asset") is not None)
        self.setWindowTitle(f"SmartCut — {project.get('name') or '未命名项目'}")
        self._set_status(f"已打开项目 {project.get('name') or project_id}")
        if project.get("asset"):
            self.controller.load_waveform()

    def _on_asset_changed(self, asset: dict) -> None:
        self._set_status("素材已复制到工作区并完成 probe")
        self.controller.load_waveform()

    def _on_proxy_ready(self, path: str) -> None:
        if not self._showing_output:
            self.preview_panel.load_media(path, "代理预览")
        self._set_status("预览代理已就绪")

    def _on_refresh_project(self) -> None:
        if self.controller.current_project:
            self.controller.open_project(self.controller.current_project)
        else:
            self.controller.refresh_projects()

    # ── 版本/片段 ─────────────────────────────────────────────────────
    def _on_revisions(self, revisions: list[dict]) -> None:
        self.inspector_panel.set_revisions(revisions)
        if not revisions:
            self.current_ir = None
            self.timeline_panel.clear()
            self.effects_panel.clear()
            self._sync_effect_scope()
            self._timing = {
                "speeds": {},
                "transitions": [],
                "overlays": [],
                "removals": [],
            }
            self._overlay_assets = []
            self.timing_panel.set_overlay_assets([])
            self.timing_panel.clear()
            self._session.reset(None)
            return
        latest = max(int(item["revision"]) for item in revisions)
        self.inspector_panel.select_revision(latest)
        self.controller.load_revision(latest)

    def _on_revision_loaded(self, payload: dict) -> None:
        if self.current_project and payload.get("project_id") != self.current_project.get("id"):
            return
        self.current_ir = payload.get("ir")
        self.unsaved = False
        self.preview_panel.clear_loop()
        self.timeline_panel.set_ir(self.current_ir)
        self._sync_audio_tracks()
        self.inspector_panel.select_revision(int(payload["revision"]))
        self.inspector_panel.diff_browser.setPlainText("选择两个版本查看结构差异")
        ir = self.current_ir or {}
        self.audio_panel.set_audio(ir.get("audio"))
        self.effects_panel.set_effects(ir.get("effects") or [])
        self._sync_effect_scope()
        self._timing = {
            "speeds": {
                op.get("id"): float(op.get("speed") or 1.0)
                for op in (ir.get("sequence") or [])
                if op.get("id")
            },
            "transitions": deepcopy(ir.get("transitions") or []),
            "overlays": deepcopy(ir.get("overlays") or []),
            "removals": deepcopy(ir.get("removals") or []),
        }
        self._sync_timing_panel()
        self.subtitle_panel.set_cues(
            [],
            burn=bool((ir.get("export") or {}).get("burn_captions")),
        )
        self._subtitle_cues = []
        self._subtitles_dirty = False
        self.controller.load_captions(int(payload["revision"]))
        audio = ir.get("audio") or {}
        if (
            audio.get("tracks") is not None
            or audio.get("volume_db")
            or audio.get("pitch_semitones")
        ):
            self._schedule_preview_mix()
        self._session.reset(self._capture_snapshot())
        self.unsaved = False
        self._set_status(f"已加载 revision {payload['revision']}")

    def _on_artifacts(self, artifacts: list[str]) -> None:
        self.artifacts = artifacts
        self.inspector_panel.set_artifacts(artifacts)

    def _on_perception_changed(self, data: dict) -> None:
        if self.current_project is None:
            return
        workspace = Path(self.current_project["workspace_dir"])
        self.perception_panel.set_perception(data, workspace)
        self.timeline_panel.set_perception(data)
        self.timeline_panel.set_frame_manifest(
            (data or {}).get("frame_manifest") or [],
            workspace,
        )

    def _draft_payload(self) -> dict:
        if self.current_ir is None:
            raise ValueError("当前没有可编辑的 EditIR")
        segments = self.timeline_panel.segments()
        if not segments:
            raise ValueError("至少保留一个片段")
        source = (self.current_ir.get("media_facts") or {}).get("src")
        if not source:
            raise ValueError("当前 EditIR 缺少 media_facts.src")

        sequence = []
        for index, segment in enumerate(segments, start=1):
            start = float(segment["start"])
            end = float(segment["end"])
            if end <= start:
                raise ValueError(f"第 {index} 段结束时间必须大于开始时间")
            item: dict = {
                "op": "trim",
                "src": source,
                "range": {
                    "start": RationalTime.from_seconds(
                        start, rate=90_000
                    ).model_dump(),
                    "end": RationalTime.from_seconds(
                        end, rate=90_000
                    ).model_dump(),
                },
                "tag": segment.get("tag") or None,
                "confidence": segment.get("confidence"),
            }
            if segment.get("id"):
                item["id"] = segment["id"]
            speed = float(
                (self._timing.get("speeds") or {}).get(
                    segment.get("id") or "", 1.0
                )
            )
            if speed != 1.0:
                item["speed"] = speed
            sequence.append(item)

        payload = dict(self.current_ir)
        payload["revision"] = 0
        payload["sequence"] = sequence
        payload["effects"] = self.effects_panel.effects()
        timing_errors = self.timing_panel.errors()
        if timing_errors:
            raise ValueError(timing_errors[0])
        payload["transitions"] = deepcopy(self._timing.get("transitions") or [])
        payload["overlays"] = deepcopy(self._timing.get("overlays") or [])
        payload["removals"] = deepcopy(self._timing.get("removals") or [])
        segment_ids_order = [item.get("id") for item in sequence]
        valid_ids = {segment_id for segment_id in segment_ids_order if segment_id}
        last_id = segment_ids_order[-1] if segment_ids_order else None
        for transition in payload["transitions"]:
            after = transition.get("after")
            if after not in valid_ids:
                raise ValueError(f"转场引用了已删除的片段: {after}")
            if after == last_id:
                raise ValueError("最后一个片段之后不能设置转场")

        segment_ids = {item.get("id") for item in sequence if item.get("id")}
        for effect in payload["effects"]:
            if effect.get("target") != "segment":
                continue
            segment_id = effect.get("segment_id")
            if segment_id not in segment_ids:
                raise ValueError(
                    f"特效引用了已删除的片段: {segment_id}；请删除或改绑该特效"
                )

        audio_errors = self.audio_panel.errors()
        if audio_errors:
            raise ValueError(audio_errors[0])
        if self._subtitles_dirty:
            errors = self.subtitle_panel.errors()
            if errors:
                raise ValueError(errors[0])
            payload["subtitles"] = [
                {
                    "start": RationalTime.from_seconds(
                        float(cue["start"]), rate=90_000
                    ).model_dump(),
                    "end": RationalTime.from_seconds(
                        float(cue["end"]), rate=90_000
                    ).model_dump(),
                    "text": str(cue["text"]),
                }
                for cue in self.subtitle_panel.cues()
            ]
        return payload

    def _build_draft_ir(self) -> EditIR:
        return EditIR.model_validate(self._draft_payload())

    def _on_save_revision(self) -> None:
        if self.current_ir is None:
            QMessageBox.information(self, "SmartCut", "当前没有可编辑的 EditIR。")
            return
        try:
            ir = self._build_draft_ir()
        except Exception as exc:
            QMessageBox.critical(self, "无法保存版本", str(exc))
            return

        self.controller.submit_revision(ir)
        self._set_status("正在保存新版本…")

    def _on_dry_run(self, revision_no: int) -> None:
        self.controller.dry_run(revision_no)
        self._set_status(f"正在编译 revision {revision_no} 的 ToolCall 计划…")

    def _on_render(self, revision_no: int) -> None:
        self.controller.render(revision_no)
        self.progress.setValue(1)
        self._set_status(f"正在渲染 revision {revision_no}…")

    def _on_export(self) -> None:
        if self.current_project is None:
            QMessageBox.information(self, "SmartCut", "请先创建或选择项目。")
            return
        revision = self.inspector_panel.current_revision()
        render_rel: str | None = None
        if revision is not None:
            candidate = (
                Path(self.current_project["workspace_dir"])
                / "renders"
                / f"r{revision}"
                / "output.mp4"
            )
            if candidate.is_file():
                render_rel = f"renders/r{revision}/output.mp4"
        dialog = ExportDialog(render_rel=render_rel, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                request = dialog.request()
            except ValueError as exc:
                QMessageBox.warning(self, "导出参数错误", str(exc))
                return
            self.controller.transcode(request)
            self._set_status("正在提交转码任务…")

    # ── Orchestrator ──────────────────────────────────────────────────
    def _on_orchestrate(
        self,
        goal: str,
        allow_render: bool,
        outline_first: bool = False,
    ) -> None:
        if self.current_project is None:
            QMessageBox.information(self, "SmartCut", "请先创建或选择项目。")
            return
        if not goal:
            QMessageBox.information(self, "SmartCut", "请先输入剪辑目标。")
            return
        if self.ai_panel.wants_fast_outline():
            phase = "fast_outline"
        elif self.ai_panel.wants_audio_first():
            phase = "audio_first"
        elif outline_first:
            phase = "outline"
        else:
            phase = "plan"

        pinned_effects = None
        if self.ai_panel.wants_pinned_effects():
            try:
                effects = _EFFECT_LIST.validate_python(
                    self.effects_panel.effects()
                )
            except Exception as exc:
                QMessageBox.critical(self, "锁定特效无效", str(exc))
                return
            pinned_effects = [
                effect for effect in effects if effect.enabled
            ]
            if not pinned_effects:
                QMessageBox.information(
                    self,
                    "锁定特效",
                    "当前没有已启用的特效可锁定。",
                )
                return
            if any(effect.target == "segment" for effect in pinned_effects):
                self._set_status(
                    "已锁定逐片段特效；若 AI 改动对应片段边界，运行会明确失败"
                )

        self._set_status("正在创建 Orchestrator 运行…")
        self.controller.orchestrate(
            goal,
            allow_render,
            pinned_effects=pinned_effects,
            phase=phase,
        )

    def _on_review_render(self) -> None:
        if self.current_project is None or self.current_ir is None:
            QMessageBox.information(self, "SmartCut", "请先加载一个成片 revision。")
            return
        revision = int(self.current_ir.get("revision") or 0)
        self._set_status(f"正在审片 revision {revision}…")
        self.controller.review_render(self.current_project["id"], revision)

    def _on_render_reviewed(self, review: dict) -> None:
        issues = review.get("issues") or []
        lines = [
            review.get("summary") or "审片完成",
            f"评分：{float(review.get('score') or 0):.2f}",
        ]
        lines.extend(
            f"[{item.get('severity', 'warning')}] {item.get('message', '')}"
            for item in issues[:12]
        )
        if len(issues) > 12:
            lines.append(f"… 另有 {len(issues) - 12} 个问题")
        self._set_status(review.get("summary") or "审片完成")
        self._show_nonmodal_message(
            QMessageBox.Icon.Information,
            "成片审片",
            "\n".join(lines),
        )

    def _on_outline_ready(self, outline: dict) -> None:
        if self.current_project is None:
            return
        workspace = Path(self.current_project["workspace_dir"])
        self.outline_panel.set_outline(outline, workspace)
        if outline.get("status") == "awaiting_approval":
            index = self.tabs.indexOf(self.outline_panel)
            if index >= 0:
                self.tabs.setCurrentIndex(index)
            self._set_status("大纲已生成，请确认章节后继续")

    def _on_outline_refresh(self) -> None:
        current = self.outline_panel.run_id()
        if current:
            self.controller.load_outline(current)

    def _on_outline_approve(self, chapters: list[dict]) -> None:
        run_id = self.outline_panel.run_id()
        if not run_id:
            return
        self.controller.approve_outline(run_id, chapters)
        self.ai_panel.set_running(True)
        self.progress.setValue(1)
        self._set_status("已确认大纲，正在继续规划与渲染…")

    def _on_outline_approved(self, result: dict) -> None:
        self._set_status(
            f"大纲已确认；Phase B 已提交: {result.get('run_id') or ''}"
        )

    def _on_cancel_jobs(self) -> None:
        if not self.controller.cancel_active_jobs():
            self._set_status("没有运行中的任务")
            return
        self._set_status("正在请求取消任务…")

    def _on_resume_run(self, run_id: str) -> None:
        self._set_status("正在提交续跑…")
        self.controller.resume_run(run_id)

    def _on_job_progress(self, job: dict) -> None:
        kind = job.get("kind", "task")
        if kind == "orchestrate":
            self.ai_panel.set_progress(job)
        self.progress.setValue(int(job.get("progress") or 0))
        self._set_status(
            f"{kind} · {job.get('status')} · {job.get('message') or ''}"
        )

    def _on_job_finished(self, _job_id: str, job: dict) -> None:
        current = (self.current_project or {}).get("id")
        if job.get("project_id") and current and job["project_id"] != current:
            return  # 跨项目任务只刷新列表，不污染当前预览/状态
        kind = job.get("kind", "task")
        if job.get("status") == "failed":
            if kind == "orchestrate":
                self.ai_panel.set_running(False)
            self.progress.setValue(0)
            self._on_error(job.get("error") or f"{kind} 失败")
            return

        if kind == "orchestrate":
            self.ai_panel.set_running(False)
        elif kind == "render":
            result = job.get("result") or {}
            output = result.get("output_abs")
            if output and Path(output).exists():
                self._showing_output = True
                self.preview_panel.load_media(output, "成片预览")
                self._set_status(f"渲染完成: {result.get('output_path')}")
            self.controller.refresh_artifacts()
        elif kind == "transcribe":
            result = job.get("result") or {}
            state = "（降级）" if result.get("degraded") else ""
            self._set_status(f"转写完成{state}")

    def _on_run_finished(self, run: dict) -> None:
        self.ai_panel.set_running(False)
        self.ai_panel.set_result(run)
        self.runs_panel.show_run(run)
        self.metrics_panel.set_run(run)
        status = run.get("status")
        self.progress.setValue(100 if status != "awaiting_approval" else 0)
        self._set_status(
            f"Orchestrator {status} · 置信度 {float(run.get('confidence') or 0):.2f}"
        )
        if status == "awaiting_approval" and run.get("run_id"):
            self.controller.load_outline(str(run["run_id"]))

        if self.current_project is None:
            return
        workspace = Path(self.current_project["workspace_dir"])
        artifacts = run.get("artifacts") or []
        output = next((item for item in artifacts if item.endswith("output.mp4")), None)
        if output is None:
            output = next((item for item in artifacts if item.endswith(".mp4")), None)
        if output:
            path = workspace / output
            if path.exists():
                self._showing_output = True
                self.preview_panel.load_media(path, "成片预览")

    def _on_segment_selected(self, _index: int, segment: dict) -> None:
        try:
            start = float(segment.get("start"))
            end = float(segment.get("end"))
        except (TypeError, ValueError):
            return
        self.preview_panel.set_loop(start, end)
        self._set_status(f"循环预览 {start:.2f}–{end:.2f}s；空格播放/暂停")

    def _on_captions_changed(self, payload: dict) -> None:
        if self._subtitles_dirty:
            return
        current = (self.current_ir or {}).get("revision")
        if current is not None and payload.get("revision") is not None:
            if int(current) != int(payload["revision"]):
                return
        cues = list(payload.get("captions") or [])
        self._subtitle_cues = cues
        self._subtitles_dirty = False
        export = (self.current_ir or {}).get("export") or {}
        self.subtitle_panel.set_cues(cues, burn=bool(export.get("burn_captions")))
        self.preview_panel.set_captions(cues)
        if cues:
            self._set_status(f"字幕预览：{len(cues)} 条（revision {payload.get('revision')}）")

    def _on_subtitles_changed(self, cues: list[dict]) -> None:
        if self.current_ir is None:
            return
        self._subtitle_cues = list(cues)
        self._subtitles_dirty = True
        self.preview_panel.set_captions(list(cues))
        self._record_change()
        self._set_status(f"字幕已修改（{len(cues)} 条），保存新版本后写入 EditIR")

    def _on_subtitle_add(self) -> None:
        if self.current_ir is None:
            return
        start = max(0.0, self.preview_panel.current_position())
        output_duration = sum(
            max(0.0, float(segment.get("end") or 0) - float(segment.get("start") or 0))
            for segment in self.timeline_panel.segments()
        )
        end = start + 2.0
        if output_duration > start + 0.2:
            end = min(end, output_duration)
        self.subtitle_panel.add_cue(start, end)
        self.tabs.setCurrentWidget(self.subtitle_panel)

    def _on_subtitle_reload(self) -> None:
        if self.current_ir is None:
            return
        revision = int(self.current_ir.get("revision") or 0)
        if revision <= 0:
            return
        self._subtitles_dirty = False
        self.controller.load_captions(revision)

    def _on_burn_changed(self, checked: bool) -> None:
        if self.current_ir is None:
            return
        export = dict(self.current_ir.get("export") or {})
        export["burn_captions"] = bool(checked)
        self.current_ir["export"] = export
        self._record_change()
        self._set_status("烧录字幕已开启" if checked else "烧录字幕已关闭")

    def _on_audio_changed(self, payload: dict) -> None:
        if self.current_ir is None:
            return
        audio = dict(self.current_ir.get("audio") or {})
        audio["volume_db"] = float(payload.get("volume_db") or 0.0)
        audio["pitch_semitones"] = float(payload.get("pitch_semitones") or 0.0)
        self.current_ir["audio"] = audio
        self._record_change()
        self._set_status(
            f"全局音频：{audio['volume_db']:+.1f} dB / "
            f"{audio['pitch_semitones']:+.0f} 半音（渲染时生效）"
        )
        self._schedule_preview_mix()

    def _on_audio_assets_changed(self, assets: list[dict]) -> None:
        self.audio_panel.set_assets(assets)
        if self.current_project is not None:
            self.audio_panel.set_context(
                main_duration=self._main_duration(self.current_project)
            )
        self._sync_audio_tracks()
        self.controller.load_audio_waveforms()

    def _main_duration(self, project: dict) -> float:
        facts = ((project or {}).get("asset") or {}).get("facts") or {}
        return seconds_or_none(facts.get("duration")) or 0.0

    def _sync_audio_tracks(self) -> None:
        if self.current_ir is None:
            return
        audio = self.current_ir.get("audio") or {}
        self.audio_panel.set_tracks(
            audio.get("tracks"),
            main_duration=(
                self._main_duration(self.current_project)
                if self.current_project
                else 0.0
            ),
        )
        self.timeline_panel.set_audio_tracks(self.audio_panel.tracks())

    def _on_audio_tracks_changed(self, tracks: list[dict]) -> None:
        if self.current_ir is None:
            return
        audio = dict(self.current_ir.get("audio") or {})
        audio["tracks"] = [
            {
                "id": str(track["id"]),
                "role": str(track.get("role") or "other"),
                "asset_id": str(track.get("asset_id") or "main"),
                "name": track.get("name") or None,
                "source_start": RationalTime.from_seconds(
                    float(track.get("source_start") or 0), rate=90_000
                ).model_dump(),
                "source_end": (
                    RationalTime.from_seconds(
                        float(track["source_end"]), rate=90_000
                    ).model_dump()
                    if track.get("source_end") is not None
                    else None
                ),
                "timeline_start": RationalTime.from_seconds(
                    float(track.get("timeline_start") or 0), rate=90_000
                ).model_dump(),
                "gain_db": float(track.get("gain_db") or 0.0),
                "muted": bool(track.get("muted")),
                "solo": bool(track.get("solo")),
                "loop": bool(track.get("loop")),
                "fade_in": RationalTime.from_seconds(
                    float(track.get("fade_in") or 0), rate=90_000
                ).model_dump(),
                "fade_out": RationalTime.from_seconds(
                    float(track.get("fade_out") or 0), rate=90_000
                ).model_dump(),
            }
            for track in tracks
        ]
        self.current_ir["audio"] = audio
        self.timeline_panel.set_audio_tracks(tracks)
        self._record_change()
        self._set_status(f"音轨已修改（{len(tracks)} 条），保存新版本后生效")
        self._schedule_preview_mix()

    def _schedule_preview_mix(self) -> None:
        if self.current_ir is None or self.current_project is None:
            return
        self._preview_mix_timer.start()

    def _request_preview_mix(self) -> None:
        if self.current_ir is None or self.current_project is None:
            return
        try:
            ir = EditIR.model_validate(self.current_ir)
        except Exception as exc:
            self._set_status(f"混音预览参数无效: {exc}")
            return
        self.controller.build_preview_mix(ir)
        self._set_status("正在生成混音预览…")

    def _on_preview_mix_ready(self, result: dict) -> None:
        if result.get("legacy"):
            return
        path = result.get("path")
        if path and Path(path).exists():
            self.preview_panel.load_media(path, "混音预览")
            state = "缓存命中" if result.get("cached") else "已生成"
            self._set_status(f"混音预览{state}")

    def _sync_timing_panel(self) -> None:
        self.timing_panel.set_data(
            self.timeline_panel.segments(),
            self._timing.get("speeds") or {},
            self._timing.get("transitions") or [],
            self._timing.get("overlays") or [],
            self._timing.get("removals") or [],
            self._overlay_assets,
        )
        self._timing = self.timing_panel.payload()

    def _on_timing_changed(self, payload: dict) -> None:
        if self.current_ir is None:
            return
        self._timing = dict(payload or {})
        self._record_change()
        self._set_status("转场/变速/水印已修改（未保存）")
        self._schedule_effects_preview()

    def _sync_effect_scope(self) -> None:
        segments = self.timeline_panel.segments()
        self.effects_panel.set_segments(segments)
        ids = {
            str(effect.get("segment_id"))
            for effect in self.effects_panel.effects()
            if effect.get("target") == "segment"
            and effect.get("enabled", True)
            and effect.get("segment_id")
        }
        self.timeline_panel.set_effect_segments(ids)

    def _on_effects_changed(self, effects: list[dict]) -> None:
        if self.current_ir is None:
            return
        self._record_change()
        self._sync_effect_scope()
        self._set_status(
            f"特效已修改（{len(effects)} 个），保存新版本后生效"
        )
        self._schedule_effects_preview()

    def _schedule_effects_preview(self) -> None:
        if self.current_ir is None or self.current_project is None:
            return
        self._effects_preview_timer.start()

    def _request_effects_preview(self) -> None:
        if self.current_ir is None or self.current_project is None:
            return
        try:
            ir = self._build_draft_ir()
        except Exception as exc:
            self._set_status(f"特效预览参数无效: {exc}")
            return
        self.controller.preview_effects(ir)
        self._set_status("正在渲染特效预览（480p）…")

    def _on_effects_preview_ready(self, result: dict) -> None:
        if result.get("cancelled"):
            return
        if result.get("error"):
            self._set_status(f"特效预览失败: {result['error']}")
            return
        if result.get("legacy"):
            self._set_status("当前没有可预览的特效")
            return
        path = result.get("path")
        if path and Path(path).exists():
            self._showing_output = True
            self.preview_panel.load_media(path, "特效预览（480p）")
            state = "缓存命中" if result.get("cached") else "已生成"
            warnings = result.get("warnings") or []
            suffix = f"（{len(warnings)} 条警告）" if warnings else ""
            self._set_status(f"特效预览{state}{suffix}")

    def _on_import_overlay_asset(self) -> None:
        if self.current_project is None:
            QMessageBox.information(self, "SmartCut", "请先创建或选择项目。")
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "导入图片/Logo 水印",
            "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*.*)",
        )
        if path:
            self.controller.import_overlay_asset(path)
            self._set_status("图片水印资产导入中…")

    def _on_overlay_assets_changed(self, assets: list[dict]) -> None:
        self._overlay_assets = list(assets or [])
        if self.current_project is not None:
            self.current_project["overlay_assets"] = self._overlay_assets
        self.timing_panel.set_overlay_assets(self._overlay_assets)
        self._set_status(f"图片水印资产：{len(self._overlay_assets)} 个")

    def _on_import_audio(self) -> None:
        if self.current_project is None:
            QMessageBox.information(self, "SmartCut", "请先创建或选择项目。")
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "导入音频（BGM / 配音 / 分离 stem）",
            "",
            "音频文件 (*.wav *.mp3 *.m4a *.aac *.flac *.ogg);;所有文件 (*.*)",
        )
        if path:
            self.controller.import_audio(path)
            self._set_status("音频导入中…")

    def _on_separate_audio(self) -> None:
        if self.current_project is None:
            QMessageBox.information(self, "SmartCut", "请先创建或选择项目。")
            return
        if not self.controller.separation_available:
            QMessageBox.information(
                self,
                "人声分离",
                "未检测到 Demucs。\n\n"
                "请安装 demucs，或设置环境变量 SMARTCUT_DEMUCS_COMMAND 指向外部命令。",
            )
            return
        self.controller.separate_audio()
        self._set_status("正在提交人声分离任务…")

    def _on_diff_changed(self, diff: dict) -> None:
        self.inspector_panel.show_diff(diff)
        self.timeline_panel.set_diff(diff)
        self._set_status(
            f"版本差异：+{diff.get('added_count', 0)} "
            f"-{diff.get('removed_count', 0)} "
            f"={diff.get('unchanged_count', 0)}"
        )

    def _step_frames(self, direction: int, *, coarse: bool = False) -> None:
        if self.current_ir is None:
            return
        facts = self.current_ir.get("media_facts") or {}
        duration = seconds_or_none(facts.get("duration")) or 0.0
        try:
            fps = fps_from_string(str(facts.get("fps") or "30/1"))
            step = 1.0 if coarse else float(1 / fps)
        except (ValueError, ZeroDivisionError):
            step = 1.0 if coarse else 0.04
        position = self.preview_panel.current_position()
        if self.preview_panel.player is None:
            position = self.timeline_panel.position
        target = max(0.0, min(duration, position + direction * step))
        self.preview_panel.seek(target)
        self.timeline_panel.set_position(target)

    def eventFilter(self, watched, event) -> bool:
        if event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(watched, event)
        if QApplication.activeModalWidget() is not None:
            return False
        focus = QApplication.focusWidget()
        if isinstance(
            focus, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)
        ):
            return False
        if (
            isinstance(focus, QAbstractItemView)
            and focus.state() == QAbstractItemView.State.EditingState
        ):
            return False
        key = event.key()
        modifiers = event.modifiers()
        if key == Qt.Key.Key_Space:
            self.preview_panel.toggle_play()
            return True
        if key == Qt.Key.Key_Left:
            self._step_frames(-1, coarse=bool(modifiers & Qt.KeyboardModifier.ShiftModifier))
            return True
        if key == Qt.Key.Key_Right:
            self._step_frames(1, coarse=bool(modifiers & Qt.KeyboardModifier.ShiftModifier))
            return True
        if key == Qt.Key.Key_Delete:
            self.timeline_panel.delete_selected()
            return True
        if modifiers & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_Z:
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                self._redo()
            else:
                self._undo()
            return True
        if modifiers & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_Y:
            self._redo()
            return True
        if modifiers & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_S:
            self._on_save_revision()
            return True
        return False

    def _on_operation_done(self, op: str, result: Any) -> None:
        if op == "dry_run" and isinstance(result, dict):
            self._show_json("ToolCall 计划", result.get("plan"))
        elif op == "plan" and isinstance(result, dict):
            revision = result.get("revision")
            if revision is not None:
                self._set_status(f"规划完成，生成 revision {revision}")
        elif op == "revision" and isinstance(result, dict):
            revision = result.get("revision")
            if revision is not None:
                self._session.mark_saved()
                self.unsaved = False
                self._set_status(f"已保存新版本 revision {revision}")
                callback = self._pending_after_save
                self._pending_after_save = None
                if callback is not None:
                    callback()
        elif op == "ingest":
            self._set_status("素材已复制到工作区")
        elif op == "transcribe" and isinstance(result, dict):
            state = "（降级）" if result.get("degraded") else ""
            self._set_status(f"转写完成{state}")
        elif op == "separation" and isinstance(result, dict):
            stems = result.get("stems") or []
            self._set_status(
                f"人声分离完成：{len(stems)} 个 stem 已加入音频素材"
                + ("（缓存命中）" if result.get("cached") else "")
            )
        elif op == "orchestrate_create" and isinstance(result, dict):
            current = (self.current_project or {}).get("id")
            if current and result.get("project_id") != current:
                return
            run_id = result.get("run_id")
            if run_id:
                self.ai_panel.set_running(True)
                self.progress.setValue(1)
                pinned = int(result.get("pinned_count") or 0)
                lock = f"，已锁定 {pinned} 个特效" if pinned else ""
                self._set_status(
                    f"Orchestrator 已提交（{result.get('phase') or 'plan'}{lock}）: {run_id}"
                )
        elif op == "resume_run" and isinstance(result, dict):
            current = (self.current_project or {}).get("id")
            if current and result.get("project_id") != current:
                return
            run_id = result.get("run_id")
            if run_id:
                self.ai_panel.set_running(True)
                self.progress.setValue(1)
                self._set_status(f"续跑已提交: {run_id}")
        elif op == "cancel_jobs" and isinstance(result, dict):
            count = int(result.get("count") or 0)
            self._set_status(
                f"已请求取消 {count} 个任务" if count else "没有运行中的任务"
            )
        elif op == "job_submit" and isinstance(result, dict):
            current = (self.current_project or {}).get("id")
            if current and result.get("project_id") != current:
                return
            job_id = result.get("job_id")
            kind = result.get("kind")
            if kind == "separate" and job_id:
                self._set_status(
                    f"人声分离任务已提交（{job_id}）；"
                    "完成后在音频面板添加人声/伴奏音轨"
                )
            elif kind == "transcode" and job_id:
                self._set_status(
                    f"转码任务已提交（{job_id}），完成后可在产物目录查看。"
                )

    # ── 菜单/对话框 ───────────────────────────────────────────────────
    def _on_new_project(self) -> None:
        name, ok = QInputDialog.getText(
            self,
            "新建项目",
            "项目名称：",
            text="未命名项目",
        )
        if ok and name.strip():
            self.controller.create_project(name.strip())

    def _on_import(self) -> None:
        if self.current_project is None:
            QMessageBox.information(self, "SmartCut", "请先创建或选择项目。")
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "导入素材（会复制进项目工作区）",
            "",
            "媒体文件 (*.mp4 *.mov *.mkv *.avi *.m4v *.wav *.mp3);;所有文件 (*.*)",
        )
        if path:
            self.controller.import_media(path)

    def _on_upload(self) -> None:
        if self.current_project is None:
            QMessageBox.information(self, "SmartCut", "请先创建或选择项目。")
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "上传素材（写入项目工作区副本）",
            "",
            "媒体文件 (*.mp4 *.mov *.mkv *.avi *.m4v *.wav *.mp3);;所有文件 (*.*)",
        )
        if path:
            self.controller.upload_media(path)

    def _on_transcribe(self) -> None:
        if self.current_project is None:
            QMessageBox.information(self, "SmartCut", "请先创建或选择项目。")
            return
        self.controller.transcribe()
        self._set_status("转写任务已提交…")

    def _on_open_output(self) -> None:
        if self.current_project is None:
            return
        folder = Path(self.current_project["workspace_dir"]) / "renders"
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _on_settings(self) -> None:
        dialog = SettingsDialog(
            self.config,
            self.settings,
            self.store,
            self.data_dir,
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        (
            config,
            api_key,
            clear_key,
            vision_key,
            clear_vision_key,
        ) = dialog.values()
        # 对话框只维护可编辑项，窗口布局等运行时状态需从当前配置合并。
        config.window_geometry = self.config.window_geometry
        config.window_state = self.config.window_state
        config.splitter_sizes = self.config.splitter_sizes
        config.center_splitter_sizes = self.config.center_splitter_sizes
        config.tab_index = self.config.tab_index
        config.last_project_id = self.config.last_project_id
        config.preview_volume = self.config.preview_volume
        config.preview_muted = self.config.preview_muted
        self.store.save(config)

        if clear_key:
            self.store.set_api_key(None)
            self.settings.deepseek_api_key = None
        elif api_key:
            persisted = self.store.set_api_key(api_key)
            self.settings.deepseek_api_key = api_key
            if not persisted:
                QMessageBox.warning(
                    self,
                    "SmartCut",
                    "当前环境无法写入 keyring，Key 仅在本次会话有效。",
                )
        if clear_vision_key:
            self.store.set_vision_api_key(None)
            self.settings.vision_api_key = None
        elif vision_key:
            persisted = self.store.set_vision_api_key(vision_key)
            self.settings.vision_api_key = vision_key
            if not persisted:
                QMessageBox.warning(
                    self,
                    "SmartCut",
                    "当前环境无法写入 keyring，视觉 Key 仅在本次会话有效。",
                )

        self.settings.deepseek_base_url = config.deepseek_base_url
        self.settings.deepseek_model = config.deepseek_model
        self.settings.orchestrator_max_parallel = config.orchestrator_max_parallel
        self.settings.orchestrator_use_llm = config.orchestrator_use_llm
        self.settings.llm_decision_summary = config.llm_decision_summary
        self.settings.hardware_encoding = config.hardware_encoding
        self.settings.vision_base_url = config.vision_base_url or None
        self.settings.vision_model = config.vision_model or None
        self.settings.demucs_command = (
            config.demucs_command or bundled_separator()
        )
        self.settings.demucs_model = config.demucs_model or "htdemucs"
        self.settings.perception_max_frames = config.perception_max_frames
        self.settings.transcribe_workers = config.transcribe_workers
        self.settings.transcribe_silence_rms = config.transcribe_silence_rms
        self.settings.transcribe_silence_min_seconds = config.transcribe_silence_min_seconds
        self.settings.audio_filler_words = config.audio_filler_words
        self.settings.caption_font_size = config.caption_font_size
        self.settings.caption_color = config.caption_color
        self.settings.caption_position = config.caption_position
        self.preview_panel.set_caption_style(
            font_size=config.caption_font_size,
            color=config.caption_color,
            position=config.caption_position,
        )
        self.config = config
        app = QApplication.instance()
        if app is not None:
            apply_theme(
                app,
                config.theme,
                scale=config.ui_scale,
                contrast=config.high_contrast,
            )
        self._set_status("设置已保存（转写模型变更需重启）")

    def _on_error(self, message: str) -> None:
        """后台任务错误只做非阻塞提示；离屏环境绝不弹模态框。"""
        text = str(message or "未知错误")
        _LOG.error("desktop_error", error=text)
        self._set_status(f"错误：{text}")
        self.status_label.setStyleSheet("color: #e5484d;")
        self.status_label.setToolTip(text)
        self._append_error_log(text)
        if not _headless_ui():
            self._show_nonmodal_error(text)

    def _append_error_log(self, message: str) -> None:
        panel = getattr(self, "diagnostics_panel", None)
        if panel is None:
            return
        stamp = time.strftime("%H:%M:%S")
        panel.logs.appendPlainText(f"[{stamp}] ERROR {message}")

    def _show_nonmodal_message(self, icon, title: str, message: str) -> None:
        if _headless_ui():
            _LOG.info(
                "desktop_dialog_suppressed",
                title=title,
                size=len(message),
            )
            return
        box = QMessageBox(self)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText(message)
        box.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setModal(False)
        box.finished.connect(lambda _=0: box.deleteLater())
        box.show()

    def _show_nonmodal_error(self, message: str) -> None:
        if self._error_box is not None:
            try:
                self._error_box.close()
            except RuntimeError:
                pass
            self._error_box = None
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("SmartCut")
        box.setText(message)
        box.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setModal(False)
        box.finished.connect(lambda _=0: box.deleteLater())
        box.show()
        self._error_box = box

    def _on_about(self) -> None:
        QMessageBox.about(
            self,
            "关于 SmartCut",
            "SmartCut 桌面客户端\n\n"
            "AI 写时间线、Orchestrator 调度、ffmpeg 编译、用户审时间线。\n"
            "除 DeepSeek 外全部本地运行。",
        )

    def _show_json(self, title: str, payload: Any) -> None:
        if payload is None:
            return
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        if _headless_ui():
            _LOG.info("json_view_suppressed", title=title, size=len(text))
            return
        if self._json_dialog is not None:
            try:
                self._json_dialog.close()
            except RuntimeError:
                pass
            self._json_dialog = None
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(900, 600)
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser(dialog)
        browser.setPlainText(text)
        layout.addWidget(browser)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.finished.connect(lambda _=0: self._clear_json_dialog(dialog))
        dialog.show()
        self._json_dialog = dialog

    def _clear_json_dialog(self, dialog: QDialog) -> None:
        if self._json_dialog is dialog:
            self._json_dialog = None
        dialog.deleteLater()

    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)
        self.status_label.setStyleSheet("")
        self.status_label.setToolTip("")

    def closeEvent(self, event) -> None:
        decision = self._resolve_unsaved("退出")
        if decision == "cancel":
            event.ignore()
            return
        if decision == "save":
            self._pending_after_save = lambda: self.close()
            self._on_save_revision()
            event.ignore()
            return
        if self.controller.has_active_jobs() and not _headless_ui():
            answer = QMessageBox.question(
                self,
                "退出 SmartCut",
                "仍有任务在运行，确定退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._capture_window_state()
        self.store.save(self.config)
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        event.accept()
