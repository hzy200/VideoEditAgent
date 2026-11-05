"""媒体工具面板：诊断、修复、转换/压缩的手动入口。

只发信号并由 MainWindow 转给 Controller；不直接调用 Services，
也不拼接任何 ffmpeg 命令。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


class MediaToolsPanel(QWidget):
    import_requested = Signal()
    diagnose_requested = Signal()
    repair_requested = Signal()
    convert_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        intro = QLabel(
            "对项目素材做结构修复、格式转换或体积压缩；源文件只读，"
            "结果写入 exports/ 或 repairs/。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("源"))
        self.source = QComboBox()
        self.source.addItem("项目主素材", None)
        row.addWidget(self.source, 1)
        layout.addLayout(row)

        action_row = QHBoxLayout()
        self.import_button = QPushButton("导入文件…")
        self.diagnose_button = QPushButton("诊断当前素材")
        self.repair_button = QPushButton("修复…")
        self.convert_button = QPushButton("转换 / 压缩…")
        action_row.addWidget(self.import_button)
        action_row.addWidget(self.diagnose_button)
        action_row.addWidget(self.repair_button)
        action_row.addWidget(self.convert_button)
        layout.addLayout(action_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.status = QLabel("等待操作")
        self.status.setObjectName("mutedText")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.result = QTextBrowser()
        self.result.setMinimumHeight(120)
        layout.addWidget(self.result, 1)

        self._render_rel: str | None = None
        self._audio_assets: list[dict] = []
        self._extra_sources: list[dict] = []
        self._project_enabled = False

        self.import_button.clicked.connect(self.import_requested.emit)
        self.diagnose_button.clicked.connect(self.diagnose_requested.emit)
        self.repair_button.clicked.connect(self.repair_requested.emit)
        self.convert_button.clicked.connect(self.convert_requested.emit)
        self.set_project_enabled(False)

    def set_project_enabled(self, enabled: bool) -> None:
        self._project_enabled = enabled
        for button in (
            self.import_button,
            self.diagnose_button,
            self.repair_button,
            self.convert_button,
        ):
            button.setEnabled(enabled)

    def _rebuild_sources(self, current: object = None) -> None:
        self.source.blockSignals(True)
        self.source.clear()
        self.source.addItem("项目主素材", None)
        if self._render_rel:
            self.source.addItem("当前成片", "__render__")
        for item in self._extra_sources:
            path = item.get("path")
            if path:
                name = item.get("name") or path
                self.source.addItem(f"导入文件：{name}", path)
        for item in self._audio_assets:
            path = item.get("path")
            if not path:
                continue
            name = item.get("name") or path
            self.source.addItem(f"音频资产：{name}", path)
        self.source.blockSignals(False)
        index = self.source.findData(current)
        if index >= 0:
            self.source.setCurrentIndex(index)

    def clear_sources(self) -> None:
        self._render_rel = None
        self._extra_sources = []
        self._audio_assets = []
        self._rebuild_sources()

    def add_source(self, path: str, name: str | None = None) -> None:
        self._extra_sources.append(
            {"path": path, "name": name or Path(path).name}
        )
        self._rebuild_sources(path)

    def set_audio_assets(self, items: list[dict]) -> None:
        self._audio_assets = list(items)
        self._rebuild_sources(self.source.currentData())

    def set_render_rel(self, render_rel: str | None) -> None:
        self._render_rel = render_rel or None
        self._rebuild_sources(self.source.currentData())

    def selected_source(self) -> str | None:
        value = self.source.currentData()
        if value == "__render__":
            return self._render_rel
        return value

    def set_running(self, running: bool) -> None:
        enabled = self._project_enabled and not running
        for button in (
            self.import_button,
            self.diagnose_button,
            self.repair_button,
            self.convert_button,
        ):
            button.setEnabled(enabled)
        if running:
            self.status.setText("媒体任务运行中…")

    def set_progress(self, job: dict) -> None:
        self.progress.setValue(int(job.get("progress") or 0))
        self.status.setText(
            f"{job.get('kind', 'task')} · {job.get('status')} · "
            f"{job.get('message') or ''}"
        )

    def show_diagnosis(self, diagnosis: dict) -> None:
        self.progress.setValue(100 if diagnosis.get("probe_ok") else 0)
        if not diagnosis.get("probe_ok"):
            self.status.setText("诊断失败：文件可能无法打开")
        else:
            self.status.setText(
                f"推荐策略：{diagnosis.get('recommended') or 'auto'}"
            )
        lines = [f"源：{diagnosis.get('src') or '-'}"]
        issues = diagnosis.get("issues") or []
        if not issues:
            lines.append("未发现明显的结构性问题。")
        for issue in issues:
            lines.append(
                f"- [{issue.get('severity', 'warning')}] "
                f"{issue.get('message', issue.get('code', ''))}"
            )
        for warning in diagnosis.get("warnings") or []:
            lines.append(f"! {warning}")
        self.result.setPlainText("\n".join(lines))

    def show_result(self, result: dict) -> None:
        if not result:
            return
        self.progress.setValue(100)
        output = result.get("output_path") or result.get("output_abs") or "-"
        self.status.setText(f"完成：{output}")
        lines = [
            f"操作：{result.get('operation', 'media_repair')}",
            f"输出：{output}",
        ]
        if result.get("cached"):
            lines.append("命中缓存")
        if result.get("used_strategy"):
            lines.append(f"实际策略：{result['used_strategy']}")
        for warning in result.get("warnings") or []:
            lines.append(f"! {warning}")
        after = result.get("diagnosis_after") or {}
        if after.get("recommended"):
            lines.append(f"修复后推荐：{after['recommended']}")
        self.result.setPlainText("\n".join(lines))
