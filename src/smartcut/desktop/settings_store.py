"""桌面配置：非敏感项写 JSON，DeepSeek Key 优先存 Windows Credential Manager。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from smartcut.logging import get_logger

KEYRING_SERVICE = "SmartCut"
KEYRING_USER = "deepseek_api_key"
KEYRING_VISION_USER = "vision_api_key"


@dataclass
class DesktopConfig:
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    whisper_model: str = "base"
    orchestrator_max_parallel: int = 4
    orchestrator_use_llm: bool = False
    llm_decision_summary: bool = False
    hardware_encoding: str = "auto"
    vision_base_url: str = ""
    vision_model: str = ""
    demucs_command: str = ""
    demucs_model: str = "htdemucs"
    perception_max_frames: int = 1200
    transcribe_workers: int = 0
    transcribe_silence_rms: float = 0.002
    transcribe_silence_min_seconds: float = 0.5
    audio_filler_words: str = "um,uh,erm,嗯,呃,那个"
    caption_font_size: int = 22
    caption_color: str = "#FFFFFF"
    caption_position: str = "bottom"
    export_preset: str = "mp4_h264"
    theme: str = "dark"
    ui_scale: float = 1.0
    high_contrast: bool = False
    language: str = "zh"
    window_geometry: str | None = None
    window_state: str | None = None
    splitter_sizes: list[int] | None = None
    center_splitter_sizes: list[int] | None = None
    tab_index: int = 0
    last_project_id: str | None = None
    preview_volume: int = 80
    preview_muted: bool = False


_LOG = get_logger("smartcut.desktop.settings_store")


class SettingsStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "desktop.json"
        self._keyring: Any | None = None
        try:
            import keyring

            self._keyring = keyring
        except Exception as exc:
            _LOG.debug("keyring_unavailable", error=str(exc))
            self._keyring = None

    @property
    def keyring_available(self) -> bool:
        return self._keyring is not None

    def load(self) -> DesktopConfig:
        if not self.path.exists():
            return DesktopConfig()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return DesktopConfig()
        allowed = {item.name for item in fields(DesktopConfig)}
        return DesktopConfig(**{key: value for key, value in payload.items() if key in allowed})

    def save(self, config: DesktopConfig) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(config), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def get_api_key(self) -> str | None:
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
                if value:
                    return value
            except Exception as exc:
                _LOG.debug("keyring_read_failed", user=KEYRING_USER, error=str(exc))
        return os.getenv("DEEPSEEK_API_KEY")

    def set_api_key(self, value: str | None) -> bool:
        return self._set_key(KEYRING_USER, value)

    def get_vision_api_key(self) -> str | None:
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(KEYRING_SERVICE, KEYRING_VISION_USER)
                if value:
                    return value
            except Exception as exc:
                _LOG.debug(
                    "keyring_read_failed",
                    user=KEYRING_VISION_USER,
                    error=str(exc),
                )
        return os.getenv("VISION_API_KEY") or os.getenv("SMARTCUT_VISION_API_KEY")

    def set_vision_api_key(self, value: str | None) -> bool:
        return self._set_key(KEYRING_VISION_USER, value)

    def _set_key(self, user: str, value: str | None) -> bool:
        if self._keyring is None:
            return False
        try:
            if value:
                self._keyring.set_password(KEYRING_SERVICE, user, value)
            else:
                try:
                    self._keyring.delete_password(KEYRING_SERVICE, user)
                except Exception as exc:
                    _LOG.debug("keyring_delete_failed", user=user, error=str(exc))
            return True
        except Exception as exc:
            _LOG.debug("keyring_write_failed", user=user, error=str(exc))
            return False
