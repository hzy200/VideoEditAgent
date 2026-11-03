"""ffprobe 是唯一读取媒体事实的入口。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from smartcut.contracts.media import MediaFacts
from smartcut.perception.probe import parse_ffprobe
from smartcut.runner.tools.base import ToolContext


class ProbeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, ProbeArgs)
    path = ctx.workspace.resolve_input(args.src)
    return [
        ctx.settings.ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]


def run_probe(executor: Any, ctx: ToolContext, src: str) -> MediaFacts:
    """执行一次 ffprobe 并解析为 MediaFacts；供 verify 与服务层复用。"""
    args = ProbeArgs(src=src)
    result = executor.run(
        build_argv(args, ctx),
        timeout=ctx.settings.tool_timeout_seconds,
        tool="probe",
    )
    payload = json.loads(result.stdout)
    return parse_ffprobe(payload, src)
