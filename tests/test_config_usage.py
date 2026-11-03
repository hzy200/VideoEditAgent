from __future__ import annotations

import re
from pathlib import Path

from pydantic import AliasChoices

from smartcut.config import Settings
from smartcut.desktop.settings_store import DesktopConfig

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "smartcut"


def _source(exclude: str) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in SRC.rglob("*.py")
        if path.name != exclude
    )


def _unused(fields: list[str], source: str) -> list[str]:
    return [
        name for name in fields if not re.search(rf"\b{re.escape(name)}\b", source)
    ]


def test_every_settings_field_is_referenced_in_production_code() -> None:
    """防止新增“死配置”：字段定义后必须在 src（config.py 之外）被消费。"""
    assert _unused(list(Settings.model_fields), _source("config.py")) == []


def test_every_desktop_config_field_is_used_outside_store() -> None:
    """desktop.json 的字段必须被界面/业务消费，不能只在 dataclass 里声明。"""
    fields = list(DesktopConfig.__dataclass_fields__)
    assert _unused(fields, _source("settings_store.py")) == []


def _settings_env_names() -> set[str]:
    names: set[str] = set()
    for field, info in Settings.model_fields.items():
        alias = info.validation_alias
        if isinstance(alias, AliasChoices):
            names.update(str(choice) for choice in alias.choices)
        else:
            names.add(f"SMARTCUT_{field.upper()}")
    return names


def test_env_example_only_documents_known_settings() -> None:
    """`.env.example` 的每个变量都必须能映射到 Settings 字段。"""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^([A-Z0-9_]+)=", text, re.MULTILINE))
    unknown = documented - _settings_env_names()
    assert unknown == set()
