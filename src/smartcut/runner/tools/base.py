"""工具契约基类。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from pydantic import BaseModel

from smartcut.config import Settings
from smartcut.runner.paths import Workspace


@dataclass(frozen=True)
class ToolContext:
    settings: Settings
    workspace: Workspace


BuildArgv: TypeAlias = Callable[[BaseModel, ToolContext], list[str]]
Prepare: TypeAlias = Callable[[BaseModel, ToolContext], None]


@dataclass(frozen=True)
class ToolDef:
    name: str
    args_model: type[BaseModel]
    build_argv: BuildArgv | None = None
    prepare: Prepare | None = None
    timeout_attr: str = "tool_timeout_seconds"
