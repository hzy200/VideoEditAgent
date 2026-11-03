"""Workspace 沙箱契约：所有工具路径必须落在工作区内，且不得覆盖输入。

放在 contracts 层，供 runner/planner/orchestrator 共享，避免跨层 import。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from smartcut.errors import SandboxError


@dataclass(frozen=True)
class Workspace:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    @property
    def overlays_dir(self) -> Path:
        return self.root / "overlays"

    @property
    def proxy_dir(self) -> Path:
        return self.root / "proxy"

    @property
    def renders_dir(self) -> Path:
        return self.root / "renders"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"

    def resolve(self, relative: str | Path) -> Path:
        if isinstance(relative, Path):
            raw = str(relative)
        else:
            raw = relative
        if not raw or not raw.strip():
            raise SandboxError("路径不能为空")
        if "\x00" in raw:
            raise SandboxError("路径包含 NUL 字符")
        if os.path.isabs(raw):
            raise SandboxError(f"只接受 workspace 相对路径: {raw!r}")
        candidate = Path(raw)
        if candidate.drive:
            raise SandboxError(f"不允许盘符路径: {raw!r}")
        if any(part == ".." for part in candidate.parts):
            raise SandboxError(f"不允许目录穿越: {raw!r}")
        resolved = (self.root / candidate).resolve()
        if not resolved.is_relative_to(self.root):
            raise SandboxError(f"路径越出 workspace: {raw!r}")
        return resolved

    def resolve_input(self, relative: str | Path) -> Path:
        path = self.resolve(relative)
        if not path.exists():
            raise SandboxError(f"输入不存在: {relative!r}")
        if not path.is_file():
            raise SandboxError(f"输入不是文件: {relative!r}")
        return path

    def resolve_output(
        self,
        relative: str | Path,
        *,
        reserved_inputs: set[Path] | None = None,
    ) -> Path:
        path = self.resolve(relative)
        if path == self.root:
            raise SandboxError("输出不能是 workspace 根目录")
        if reserved_inputs and path in reserved_inputs:
            raise SandboxError(f"禁止覆盖输入文件: {relative!r}")
        if path.exists() and path.is_dir():
            raise SandboxError(f"输出路径是目录: {relative!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
