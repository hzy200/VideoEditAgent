from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from smartcut.config import Settings
from smartcut.contracts.export_presets import EXPORT_PRESETS
from smartcut.desktop.settings_store import DesktopConfig, SettingsStore


class SettingsDialog(QDialog):
    def __init__(
        self,
        config: DesktopConfig,
        settings: Settings,
        store: SettingsStore,
        data_dir: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(560)

        self._whisper_model = config.whisper_model

        layout = QVBoxLayout(self)
        basic = QFormLayout()

        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText(
            "已保存在 Windows Credential Manager" if store.get_api_key() else "未配置"
        )
        basic.addRow("DeepSeek API Key", self.api_key)

        self.clear_key = QCheckBox("清除已保存的 Key")
        basic.addRow("", self.clear_key)

        self.base_url = QLineEdit(config.deepseek_base_url)
        basic.addRow("Base URL", self.base_url)

        self.model = QLineEdit(config.deepseek_model)
        basic.addRow("模型", self.model)

        self.hardware = QComboBox()
        self.hardware.addItem("自动（NVENC/QSV/AMF，失败回退）", "auto")
        self.hardware.addItem("关闭（只用 libx264）", "off")
        index = self.hardware.findData(config.hardware_encoding or "auto")
        self.hardware.setCurrentIndex(max(0, index))
        basic.addRow("硬件编码", self.hardware)

        self.export_preset = QComboBox()
        for name, preset in EXPORT_PRESETS.items():
            self.export_preset.addItem(preset.description, name)
        self.export_preset.setCurrentIndex(
            max(0, self.export_preset.findData(config.export_preset))
        )
        basic.addRow("导出预设", self.export_preset)

        self.caption_position = QComboBox()
        for label, value in (
            ("底部", "bottom"),
            ("中部", "middle"),
            ("顶部", "top"),
        ):
            self.caption_position.addItem(label, value)
        index = self.caption_position.findData(config.caption_position)
        self.caption_position.setCurrentIndex(max(0, index))
        basic.addRow("字幕位置", self.caption_position)

        self.theme = QComboBox()
        self.theme.addItem("深色", "dark")
        self.theme.addItem("浅色", "light")
        self.theme.addItem("跟随系统", "system")
        self.theme.setCurrentIndex(max(0, self.theme.findData(config.theme)))
        basic.addRow("主题", self.theme)

        layout.addLayout(basic)

        self.advanced_toggle = QCheckBox("显示高级设置")
        self.advanced_toggle.setChecked(False)
        layout.addWidget(self.advanced_toggle)

        self.advanced = QWidget()
        advanced = QFormLayout(self.advanced)
        advanced.setContentsMargins(0, 0, 0, 0)

        whisper_path = settings.whisper_model_path or "未找到内嵌模型（将使用模型名）"
        advanced.addRow("转写模型", QLabel(f"{config.whisper_model} · {whisper_path}"))
        advanced.addRow("", QLabel("修改转写模型需要重新打包或重启应用"))

        self.parallel = QSpinBox()
        self.parallel.setRange(1, 8)
        self.parallel.setValue(config.orchestrator_max_parallel)
        advanced.addRow("Orchestrator 并行度", self.parallel)

        self.use_llm = QCheckBox("LLM 动态任务图（实验；默认规则图）")
        self.use_llm.setChecked(config.orchestrator_use_llm)
        advanced.addRow("", self.use_llm)

        self.decision_summary = QCheckBox("最终决策额外调用 LLM 生成总结")
        self.decision_summary.setChecked(config.llm_decision_summary)
        advanced.addRow("", self.decision_summary)

        self.vision_base_url = QLineEdit(config.vision_base_url)
        self.vision_base_url.setPlaceholderText("OpenAI 兼容 VLM Base URL（留空禁用）")
        advanced.addRow("视觉 Base URL", self.vision_base_url)
        self.vision_model = QLineEdit(config.vision_model)
        self.vision_model.setPlaceholderText("例如 qwen-vl-plus / gpt-4o-mini")
        advanced.addRow("视觉模型", self.vision_model)
        self.vision_api_key = QLineEdit()
        self.vision_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.vision_api_key.setPlaceholderText(
            "已保存视觉 Key" if store.get_vision_api_key() else "未配置视觉 Key"
        )
        advanced.addRow("视觉 API Key", self.vision_api_key)
        self.clear_vision_key = QCheckBox("清除视觉 Key")
        advanced.addRow("", self.clear_vision_key)

        self.demucs_command = QLineEdit(config.demucs_command)
        self.demucs_command.setPlaceholderText(
            "可选：外部 demucs 命令；留空则尝试 PATH / Python 模块"
        )
        advanced.addRow("Demucs 命令", self.demucs_command)
        self.demucs_model = QLineEdit(config.demucs_model or "htdemucs")
        self.demucs_model.setPlaceholderText("htdemucs")
        advanced.addRow("Demucs 模型", self.demucs_model)

        self.perception_max_frames = QSpinBox()
        self.perception_max_frames.setRange(0, 100_000)
        self.perception_max_frames.setValue(config.perception_max_frames)
        self.perception_max_frames.setSpecialValueText("不限制")
        advanced.addRow("感知分析帧上限", self.perception_max_frames)

        self.transcribe_workers = QSpinBox()
        self.transcribe_workers.setRange(0, 8)
        self.transcribe_workers.setValue(config.transcribe_workers)
        self.transcribe_workers.setSpecialValueText("auto")
        advanced.addRow("转写 worker", self.transcribe_workers)

        self.silence_rms = QDoubleSpinBox()
        self.silence_rms.setRange(0.0, 0.2)
        self.silence_rms.setDecimals(4)
        self.silence_rms.setSingleStep(0.001)
        self.silence_rms.setValue(config.transcribe_silence_rms)
        advanced.addRow("静音 RMS 阈值", self.silence_rms)

        self.silence_min = QDoubleSpinBox()
        self.silence_min.setRange(0.0, 60.0)
        self.silence_min.setDecimals(1)
        self.silence_min.setValue(config.transcribe_silence_min_seconds)
        advanced.addRow("静音最短时长(s)", self.silence_min)

        self.filler_words = QLineEdit(config.audio_filler_words)
        self.filler_words.setPlaceholderText("逗号分隔，例如 um,uh,erm,嗯,呃")
        advanced.addRow("口癖词表", self.filler_words)

        self.caption_font_size = QSpinBox()
        self.caption_font_size.setRange(8, 96)
        self.caption_font_size.setValue(config.caption_font_size)
        advanced.addRow("字幕字号", self.caption_font_size)
        self.caption_color = QLineEdit(config.caption_color)
        self.caption_color.setPlaceholderText("#FFFFFF")
        advanced.addRow("字幕颜色", self.caption_color)

        self.ui_scale = QDoubleSpinBox()
        self.ui_scale.setRange(0.75, 2.0)
        self.ui_scale.setSingleStep(0.05)
        self.ui_scale.setDecimals(2)
        self.ui_scale.setValue(config.ui_scale)
        advanced.addRow("界面缩放", self.ui_scale)

        self.high_contrast = QCheckBox("高对比度")
        self.high_contrast.setChecked(config.high_contrast)
        advanced.addRow("", self.high_contrast)

        self.language = QComboBox()
        self.language.addItem("中文", "zh")
        self.language.addItem("English（未实现）", "en")
        item = self.language.model().item(1)
        item.setEnabled(False)
        self.language.setCurrentIndex(max(0, self.language.findData(config.language)))
        advanced.addRow("语言", self.language)

        advanced.addRow("数据目录", QLabel(str(data_dir)))
        if not store.keyring_available:
            warning = QLabel("未检测到 keyring，API Key 只能通过环境变量提供。")
            warning.setStyleSheet("color:#d97706;")
            advanced.addRow("", warning)

        self.advanced.setVisible(False)
        self.advanced_toggle.toggled.connect(self.advanced.setVisible)
        layout.addWidget(self.advanced)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[DesktopConfig, str | None, bool, str | None, bool]:
        config = DesktopConfig(
            deepseek_base_url=self.base_url.text().strip() or "https://api.deepseek.com",
            deepseek_model=self.model.text().strip() or "deepseek-chat",
            whisper_model=self._whisper_model,
            orchestrator_max_parallel=self.parallel.value(),
            orchestrator_use_llm=self.use_llm.isChecked(),
            llm_decision_summary=self.decision_summary.isChecked(),
            hardware_encoding=str(self.hardware.currentData() or "auto"),
            vision_base_url=self.vision_base_url.text().strip(),
            vision_model=self.vision_model.text().strip(),
            demucs_command=self.demucs_command.text().strip(),
            demucs_model=self.demucs_model.text().strip() or "htdemucs",
            perception_max_frames=self.perception_max_frames.value(),
            transcribe_workers=self.transcribe_workers.value(),
            transcribe_silence_rms=self.silence_rms.value(),
            transcribe_silence_min_seconds=self.silence_min.value(),
            audio_filler_words=self.filler_words.text().strip()
            or "um,uh,erm,嗯,呃,那个",
            caption_font_size=self.caption_font_size.value(),
            caption_color=self.caption_color.text().strip() or "#FFFFFF",
            caption_position=str(self.caption_position.currentData() or "bottom"),
            export_preset=str(self.export_preset.currentData() or "mp4_h264"),
            theme=str(self.theme.currentData() or "dark"),
            ui_scale=self.ui_scale.value(),
            high_contrast=self.high_contrast.isChecked(),
            language=str(self.language.currentData() or "zh"),
        )
        api_key = self.api_key.text().strip() or None
        vision_key = self.vision_api_key.text().strip() or None
        return (
            config,
            api_key,
            self.clear_key.isChecked(),
            vision_key,
            self.clear_vision_key.isChecked(),
        )
