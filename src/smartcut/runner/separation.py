"""可选分离适配器：Protocol + 外部 Demucs 实现，主进程不导入 PyTorch。"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from smartcut.cancel import CancellationToken
from smartcut.config import Settings
from smartcut.errors import RunCancelledError, ToolExecutionError, UnsupportedFeature
from smartcut.logging import get_logger
from smartcut.resources import ResourceManager
from smartcut.runner.paths import Workspace
from smartcut.runner.process_tree import popen_group_kwargs, terminate_process_tree


@dataclass(frozen=True)
class SeparationResult:
    stems: dict[str, Path]
    command: list[str]
    duration_ms: int
    model: str
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Separator(Protocol):
    """可替换的分离器；UVR/MDX 后续实现同一协议。"""

    name: str

    def available(self) -> bool: ...

    def separate(
        self,
        source: Path,
        output_dir: Path,
        *,
        model: str | None = None,
        stems: int = 2,
        cancel_token: CancellationToken | None = None,
        progress: Callable[[int, str], None] | None = None,
    ) -> SeparationResult: ...


def resolve_command(settings: Settings) -> list[str] | None:
    configured = (settings.demucs_command or "").strip()
    if configured:
        if Path(configured).is_file():
            return [configured]
        try:
            parts = shlex.split(configured)
        except ValueError as exc:
            raise UnsupportedFeature(f"SMARTCUT_DEMUCS_COMMAND 无法解析: {exc}") from exc
        return parts or None
    try:
        found = importlib.util.find_spec("demucs") is not None
    except (ImportError, ValueError):
        found = False
    if found:
        # 开发态优先用带进度/本地模型解析的 worker。
        return [sys.executable, "-m", "smartcut.separation_worker"]
    executable = shutil.which("demucs")
    if executable:
        return [executable]
    return None


def available(settings: Settings) -> bool:
    try:
        return resolve_command(settings) is not None
    except UnsupportedFeature:
        return False


def build_argv(
    settings: Settings,
    source: Path,
    output_dir: Path,
    model: str | None = None,
    *,
    stems: int = 2,
) -> list[str]:
    if stems not in {2, 4}:
        raise UnsupportedFeature(f"Demucs 适配器只支持 2/4 stems，收到 {stems}")
    command = resolve_command(settings)
    if command is None:
        raise UnsupportedFeature(
            "未配置 Demucs：安装 demucs 或设置 SMARTCUT_DEMUCS_COMMAND"
        )
    argv = [*command]
    if stems == 2:
        argv.append("--two-stems=vocals")
    argv.extend(
        [
            "-n",
            model or settings.demucs_model,
            "-o",
            str(output_dir),
            str(source),
        ]
    )
    return argv


def _stem_name(filename: str) -> str:
    return {
        "vocals": "vocals",
        "no_vocals": "instrumental",
        "minus_vocals": "instrumental",
        "accompaniment": "instrumental",
        "drums": "drums",
        "bass": "bass",
        "other": "other",
    }.get(filename, filename)


def _report_worker_progress(
    progress: Callable[[int, str], None],
    path: Path,
) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        progress(40, "Demucs 分离中…")
        return
    try:
        percent = int(payload.get("percent", 40))
    except (TypeError, ValueError):
        percent = 40
    progress(
        max(1, min(99, percent)),
        str(payload.get("message") or "Demucs 分离中…"),
    )


def _run_demucs(
    settings: Settings,
    workspace: Workspace,
    source: Path,
    output_dir: Path,
    *,
    model: str | None = None,
    stems: int = 2,
    cancel_token: CancellationToken | None = None,
    progress: Callable[[int, str], None] | None = None,
    logger: Any | None = None,
    resources: ResourceManager | None = None,
) -> SeparationResult:
    log = logger or get_logger("smartcut.separation")
    argv = build_argv(settings, source, output_dir, model, stems=stems)
    output_dir.mkdir(parents=True, exist_ok=True)
    if progress is not None:
        progress(5, "准备 Demucs 分离…")

    progress_file = output_dir / "progress.json"
    progress_file.unlink(missing_ok=True)
    env = os.environ.copy()
    env["SMARTCUT_DEMUCS_PROGRESS_FILE"] = str(progress_file)
    manager = resources or ResourceManager(settings)
    # 执行超时只覆盖真正执行的时间；排队等待 CPU 配额不计入。
    with manager.cpu_slot(cancel_token=cancel_token):
        started = time.perf_counter()
        deadline = started + settings.demucs_timeout_seconds
        proc = subprocess.Popen(
            argv,
            cwd=str(workspace.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            env=env,
            **popen_group_kwargs(),
        )
        while True:
            if cancel_token is not None and cancel_token.cancelled:
                terminate_process_tree(proc)
                raise RunCancelledError("Demucs 分离已取消")
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                terminate_process_tree(proc)
                raise ToolExecutionError(
                    f"Demucs 分离超时（>{settings.demucs_timeout_seconds}s）"
                )
            try:
                stdout, stderr = proc.communicate(timeout=min(1.0, remaining))
                break
            except subprocess.TimeoutExpired:
                if progress is not None:
                    _report_worker_progress(progress, progress_file)
                continue

    duration_ms = int((time.perf_counter() - started) * 1000)
    if proc.returncode != 0:
        tail = (stderr or stdout or "").strip().splitlines()[-8:]
        raise ToolExecutionError(
            f"Demucs 退出码 {proc.returncode}: {' | '.join(tail)}"
        )

    files = sorted(output_dir.rglob("*.wav"))
    stems_map: dict[str, Path] = {}
    for path in files:
        stems_map[_stem_name(path.stem)] = path
    if "vocals" not in stems_map:
        raise ToolExecutionError("Demucs 未生成 vocals 音轨")
    if progress is not None:
        progress(85, f"Demucs 完成：{', '.join(sorted(stems_map))}")
    selected_model = model or settings.demucs_model
    metadata: dict[str, Any] = {
        "separator": "demucs",
        "model": selected_model,
        "stems": stems,
    }
    try:
        worker_meta = json.loads(
            (output_dir / "result.json").read_text(encoding="utf-8")
        )
        if isinstance(worker_meta, dict):
            metadata.update(worker_meta)
    except (OSError, ValueError):
        pass
    log.info(
        "demucs_separated",
        stems=sorted(stems_map),
        duration_ms=duration_ms,
        model=selected_model,
        device=metadata.get("device"),
        gpu=metadata.get("gpu"),
    )
    return SeparationResult(
        stems=stems_map,
        command=argv,
        duration_ms=duration_ms,
        model=selected_model,
        metadata=metadata,
    )


class DemucsSeparator:
    name = "demucs"

    def __init__(
        self,
        settings: Settings,
        workspace: Workspace,
        *,
        resources: ResourceManager | None = None,
        logger: Any | None = None,
    ) -> None:
        self.settings = settings
        self.workspace = workspace
        self.resources = resources
        self.logger = logger

    def available(self) -> bool:
        return available(self.settings)

    def separate(
        self,
        source: Path,
        output_dir: Path,
        *,
        model: str | None = None,
        stems: int = 2,
        cancel_token: CancellationToken | None = None,
        progress: Callable[[int, str], None] | None = None,
    ) -> SeparationResult:
        return _run_demucs(
            self.settings,
            self.workspace,
            source,
            output_dir,
            model=model,
            stems=stems,
            cancel_token=cancel_token,
            progress=progress,
            logger=self.logger,
            resources=self.resources,
        )


def build_separator(
    settings: Settings,
    workspace: Workspace,
    *,
    resources: ResourceManager | None = None,
    logger: Any | None = None,
) -> Separator:
    """工厂入口；未来 UVR/MDX 可在此按配置选择。"""
    return DemucsSeparator(
        settings,
        workspace,
        resources=resources,
        logger=logger,
    )


def separate(
    settings: Settings,
    workspace: Workspace,
    source: Path,
    output_dir: Path,
    *,
    model: str | None = None,
    stems: int = 2,
    cancel_token: CancellationToken | None = None,
    progress: Callable[[int, str], None] | None = None,
    logger: Any | None = None,
    resources: ResourceManager | None = None,
) -> SeparationResult:
    """兼容旧调用；新代码应使用 build_separator().separate()."""
    return _run_demucs(
        settings,
        workspace,
        source,
        output_dir,
        model=model,
        stems=stems,
        cancel_token=cancel_token,
        progress=progress,
        logger=logger,
        resources=resources,
    )
