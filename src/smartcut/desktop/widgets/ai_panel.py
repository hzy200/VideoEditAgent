from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from smartcut.desktop.presets import SCENE_PRESETS


class AIPanel(QWidget):
    run_requested = Signal(str, bool, bool)
    cancel_requested = Signal()
    review_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        preset_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("场景预设…", None)
        for preset in SCENE_PRESETS:
            self.preset_combo.addItem(preset.name, preset.id)
        self.preset_button = QPushButton("应用预设")
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(self.preset_button)
        layout.addLayout(preset_row)

        layout.addWidget(QLabel("告诉 SmartCut 你想要什么："))
        self.goal_edit = QTextEdit()
        self.goal_edit.setPlaceholderText(
            "例如：剪成 3 条 60 秒竖版切片，去掉口癖和长停顿，保留参数讲解与售后政策"
        )
        self.goal_edit.setMaximumHeight(110)
        layout.addWidget(self.goal_edit)

        self.only_analysis = QCheckBox("只分析（不渲染）")
        layout.addWidget(self.only_analysis)
        self.outline_first = QCheckBox("先生成大纲（人工确认后继续）")
        layout.addWidget(self.outline_first)
        self.fast_outline = QCheckBox("快速大纲（跳过质量/人脸/完整转写）")
        self.fast_outline.setToolTip(
            "仅抽帧、镜头切分与 contact sheet，先生成章节；确认后再做精细分析。"
        )
        layout.addWidget(self.fast_outline)
        self.audio_first = QCheckBox("音频优先（先转写/边界，再抽帧）")
        layout.addWidget(self.audio_first)
        self.lock_effects = QCheckBox("锁定当前特效，AI 只改时间线")
        self.lock_effects.setToolTip(
            "使用“特效”面板中已启用的全局特效；规划器只能改 sequence/audio/export。"
            "锁定项与 AI 新时间线不兼容时，运行会明确失败而不是静默丢弃。"
        )
        layout.addWidget(self.lock_effects)

        options_row = QHBoxLayout()
        self.vertical = QCheckBox("9:16 竖版")
        self.burn_captions = QCheckBox("烧录字幕")
        self.loudness = QCheckBox("响度归一 -14 LUFS")
        self.remove_filler = QCheckBox("去口癖")
        options_row.addWidget(self.vertical)
        options_row.addWidget(self.burn_captions)
        options_row.addWidget(self.loudness)
        options_row.addWidget(self.remove_filler)
        options_row.addStretch(1)
        layout.addLayout(options_row)

        action_row = QHBoxLayout()
        self.run_button = QPushButton("运行 Orchestrator")
        self.run_button.setMinimumHeight(34)
        self.cancel_button = QPushButton("取消当前任务")
        self.cancel_button.setMinimumHeight(34)
        self.cancel_button.setEnabled(False)
        self.review_button = QPushButton("审片当前成片")
        self.review_button.setMinimumHeight(34)
        action_row.addWidget(self.run_button, 1)
        action_row.addWidget(self.cancel_button)
        action_row.addWidget(self.review_button)
        layout.addLayout(action_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.status_label = QLabel("空闲")
        self.status_label.setStyleSheet("color:#64748b;")
        layout.addWidget(self.status_label)

        layout.addWidget(QLabel("最终决策答案"))
        self.answer = QTextBrowser()
        self.answer.setMinimumHeight(130)
        layout.addWidget(self.answer, 1)

        layout.addWidget(QLabel("任务账本"))
        self.ledger = QTableWidget(0, 5)
        self.ledger.setHorizontalHeaderLabels(["任务", "Agent", "状态", "说明", "错误"])
        self.ledger.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.ledger.verticalHeader().setVisible(False)
        layout.addWidget(self.ledger, 1)

        self.preset_button.clicked.connect(self._apply_preset)
        self.run_button.clicked.connect(self._emit_run)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        self.review_button.clicked.connect(self.review_requested.emit)
        self.set_project_enabled(False)

    def goal_text(self) -> str:
        return self.goal_edit.toPlainText().strip()

    def allow_render(self) -> bool:
        return not self.only_analysis.isChecked()

    def wants_outline(self) -> bool:
        return self.outline_first.isChecked() or self.fast_outline.isChecked()

    def wants_fast_outline(self) -> bool:
        return self.fast_outline.isChecked()

    def wants_audio_first(self) -> bool:
        return self.audio_first.isChecked()

    def wants_pinned_effects(self) -> bool:
        return self.lock_effects.isChecked()

    def set_project_enabled(self, enabled: bool) -> None:
        self.run_button.setEnabled(enabled)

    def set_running(self, running: bool) -> None:
        self.run_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.progress.setValue(0 if running else self.progress.value())
        if running:
            self.status_label.setText("Orchestrator 运行中…")
            self.answer.clear()
            self.ledger.setRowCount(0)

    def set_progress(self, job: dict) -> None:
        self.progress.setValue(int(job.get("progress") or 0))
        message = job.get("message") or job.get("status") or ""
        self.status_label.setText(f"{job.get('kind', 'task')} · {job.get('status')} · {message}")

    def set_result(self, run: dict) -> None:
        self.progress.setValue(100)
        self.status_label.setText(
            f"{run.get('status', '-')} · 置信度 {run.get('confidence', 0):.2f}"
        )
        self.answer.setPlainText(run.get("answer") or "")
        self.cancel_button.setEnabled(False)
        self.ledger.setRowCount(0)
        for outcome in run.get("ledger") or []:
            row = self.ledger.rowCount()
            self.ledger.insertRow(row)
            values = [
                outcome.get("task_id", ""),
                outcome.get("agent", ""),
                outcome.get("status", ""),
                outcome.get("summary", ""),
                outcome.get("error") or "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 2:
                    if value == "succeeded":
                        item.setForeground(QColor("#16a34a"))
                    elif value == "failed":
                        item.setForeground(QColor("#dc2626"))
                    elif value == "skipped":
                        item.setForeground(QColor("#d97706"))
                self.ledger.setItem(row, column, item)
        self.run_button.setEnabled(True)

    def clear(self) -> None:
        self.answer.clear()
        self.ledger.setRowCount(0)
        self.progress.setValue(0)
        self.status_label.setText("空闲")

    def _apply_preset(self) -> None:
        preset_id = self.preset_combo.currentData()
        preset = next(
            (item for item in SCENE_PRESETS if item.id == preset_id),
            None,
        )
        if preset is None:
            return
        self.goal_edit.setPlainText(preset.goal)
        self.vertical.setChecked(preset.aspect == "9:16")
        self.burn_captions.setChecked(preset.burn_captions)
        self.loudness.setChecked(bool(preset.loudness))
        self.remove_filler.setChecked(preset.remove_filler)
        self.only_analysis.setChecked(not preset.allow_render)
        self.outline_first.setChecked(preset.phase == "outline")
        self.fast_outline.setChecked(preset.phase == "fast_outline")
        self.audio_first.setChecked(preset.phase == "audio_first")

    def _emit_run(self) -> None:
        extras: list[str] = []
        if self.vertical.isChecked():
            extras.append("输出 9:16 竖版")
        if self.burn_captions.isChecked():
            extras.append("烧录字幕")
        if self.loudness.isChecked():
            extras.append("响度归一 -14 LUFS")
        if self.remove_filler.isChecked():
            extras.append("去口癖、删除语气词")
        goal = self.goal_text()
        if extras:
            goal = (goal + "；要求：" if goal else "要求：") + "，".join(extras)
        self.run_requested.emit(goal, self.allow_render(), self.wants_outline())
