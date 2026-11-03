"""子进程执行与 ToolCall 分发。shell=False + 二进制白名单。"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from smartcut.cancel import CancellationToken
from smartcut.config import Settings
from smartcut.contracts.tools import ToolCall, ToolResult
from smartcut.errors import RunCancelledError, ToolExecutionError
from smartcut.logging import get_logger
from smartcut.resources import ResourceManager
from smartcut.runner.paths import Workspace
from smartcut.runner.process_tree import popen_group_kwargs, terminate_process_tree
from smartcut.runner.tools.base import ToolContext
from smartcut.runner.tools.probe import run_probe
from smartcut.runner.tools.registry import get_tool
from smartcut.runner.verify import VerifyArgs, verify_facts


def _temp_output_path(relative: str) -> str:
    """同目录临时文件；保留扩展名让 ffmpeg 正确推断容器格式。"""
    path = Path(relative)
    suffix = path.suffix
    stem = path.name[: -len(suffix)] if suffix else path.name
    return str(path.with_name(f"{stem}.part-{uuid4().hex[:8]}{suffix}"))


@dataclass(frozen=True)
class ExecResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class ProcessRunner:
    def __init__(
        self,
        settings: Settings,
        workspace: Workspace,
        logger: Any | None = None,
        *,
        resources: ResourceManager | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        self.settings = settings
        self.workspace = workspace
        self.logger = logger or get_logger("smartcut.runner")
        self.resources = resources or ResourceManager(settings)
        self.cancel_token = cancel_token

    def _check_binary(self, executable: str) -> None:
        allowed = {self.settings.ffmpeg_bin, self.settings.ffprobe_bin}
        if executable not in allowed:
            raise ToolExecutionError(f"禁止执行未授权二进制: {executable!r}")

    def run(
        self,
        argv: list[str],
        *,
        timeout: float,
        tool: str,
        cancel_token: CancellationToken | None = None,
    ) -> ExecResult:
        if not argv:
            raise ToolExecutionError("空 argv")
        self._check_binary(argv[0])
        token = cancel_token or self.cancel_token
        if token is not None:
            token.raise_if_cancelled()

        popen_kwargs = popen_group_kwargs()
        # 执行超时只覆盖真正执行的时间；排队等待资源的时间单独由
        # FairLimiter 的超时控制，避免“刚 spawn 就被判超时”。
        with self.resources.ffmpeg_slot(cancel_token=token):
            started = time.perf_counter()
            deadline = started + timeout
            proc = subprocess.Popen(
                argv,
                cwd=str(self.workspace.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                **popen_kwargs,
            )
            stdout = ""
            stderr = ""
            while True:
                if token is not None and token.cancelled:
                    stdout, stderr = terminate_process_tree(proc)
                    duration_ms = int((time.perf_counter() - started) * 1000)
                    self.logger.warning(
                        "tool_cancelled",
                        tool=tool,
                        argv=argv,
                        duration_ms=duration_ms,
                    )
                    raise RunCancelledError(f"{tool} 已取消")
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    stdout, stderr = terminate_process_tree(proc)
                    duration_ms = int((time.perf_counter() - started) * 1000)
                    self.logger.error(
                        "tool_timeout",
                        tool=tool,
                        argv=argv,
                        timeout=timeout,
                        duration_ms=duration_ms,
                    )
                    raise ToolExecutionError(f"{tool} 执行超时（>{timeout}s）")
                try:
                    stdout, stderr = proc.communicate(timeout=min(0.5, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue

        duration_ms = int((time.perf_counter() - started) * 1000)
        self.logger.info(
            "tool_exec",
            tool=tool,
            argv=argv,
            exit_code=proc.returncode,
            duration_ms=duration_ms,
        )
        if proc.returncode != 0:
            tail = (stderr or stdout or "").strip()[-2000:]
            raise ToolExecutionError(f"{tool} 退出码 {proc.returncode}: {tail}")
        return ExecResult(
            argv=argv,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
        )


class ToolCallExecutor:
    """执行单个 ToolCall；verify 由本层就地进行 ffprobe 断言。"""

    def __init__(
        self,
        settings: Settings,
        workspace: Workspace,
        logger: Any | None = None,
        *,
        resources: ResourceManager | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        self.settings = settings
        self.workspace = workspace
        self.logger = logger or get_logger("smartcut.executor")
        self.resources = resources or ResourceManager(settings)
        self.cancel_token = cancel_token
        self.process = ProcessRunner(
            settings,
            workspace,
            self.logger,
            resources=self.resources,
            cancel_token=cancel_token,
        )
        self.ctx = ToolContext(settings=settings, workspace=workspace)

    def probe(self, src: str):
        return run_probe(self.process, self.ctx, src)

    def execute(
        self,
        call: ToolCall,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        if self.cancel_token is not None:
            self.cancel_token.raise_if_cancelled()
        tool_def = get_tool(call.tool)
        args = tool_def.args_model.model_validate(call.args)

        if call.tool == "verify":
            assert isinstance(args, VerifyArgs)
            started = time.perf_counter()
            actual = self.probe(args.src)
            verify_facts(actual, args)
            duration_ms = int((time.perf_counter() - started) * 1000)
            self.logger.info(
                "verify_ok",
                src=args.src,
                expected=args.expected_duration.to_ffmpeg_seconds(),
                actual=actual.duration.to_ffmpeg_seconds(),
            )
            return ToolResult(
                tool="verify",
                argv=[],
                exit_code=0,
                duration_ms=duration_ms,
                stdout=actual.model_dump_json(),
                artifacts=[args.src],
            )

        if tool_def.build_argv is None:
            raise ToolExecutionError(f"工具 {call.tool!r} 缺少 build_argv")
        original_dst = getattr(args, "dst", None)
        temp_dst: str | None = None
        if isinstance(original_dst, str):
            # 先按最终路径做一次校验/构建，保留 reserved_inputs 等沙箱检查；
            # 再改写到同目录临时文件，成功后才原子替换最终产物。
            if tool_def.prepare is not None:
                tool_def.prepare(args, self.ctx)
            tool_def.build_argv(args, self.ctx)
            temp_dst = _temp_output_path(original_dst)
            args = args.model_copy(update={"dst": temp_dst})
        if tool_def.prepare is not None:
            tool_def.prepare(args, self.ctx)
        argv = tool_def.build_argv(args, self.ctx)
        timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else getattr(self.settings, tool_def.timeout_attr)
        )
        try:
            result = self.process.run(
                argv,
                timeout=timeout,
                tool=call.tool,
                cancel_token=self.cancel_token,
            )
            if temp_dst is not None:
                temp_abs = self.workspace.resolve_output(temp_dst)
                final_abs = self.workspace.resolve_output(original_dst)
                final_abs.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temp_abs, final_abs)
        except BaseException:
            if temp_dst is not None:
                try:
                    self.workspace.resolve_output(temp_dst).unlink(missing_ok=True)
                except OSError:
                    pass
            raise

        artifacts: list[str] = []
        if isinstance(original_dst, str):
            artifacts.append(original_dst)
        encoder = None
        raw_encoder = call.args.get("encoder")
        if isinstance(raw_encoder, dict):
            encoder = raw_encoder.get("codec")
        return ToolResult(
            tool=call.tool,
            argv=result.argv,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            stdout=result.stdout,
            stderr=result.stderr,
            artifacts=artifacts,
            encoder=encoder if isinstance(encoder, str) else None,
        )
