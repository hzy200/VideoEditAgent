"""拼接工具：片段必须由同一套编码参数产生，才允许流拷贝拼接。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.errors import SandboxError
from smartcut.runner.tools.base import ToolContext


class ConcatArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inputs: list[str] = Field(min_length=2)
    dst: str
    list_path: str


def _concat_escape(path: str) -> str:
    normalized = path.replace("\\", "/")
    return normalized.replace("'", "'\\''")


def _input_paths(args: ConcatArgs, ctx: ToolContext) -> set:
    return {ctx.workspace.resolve_input(relative) for relative in args.inputs}


def prepare(args: BaseModel, ctx: ToolContext) -> None:
    assert isinstance(args, ConcatArgs)
    inputs = _input_paths(args, ctx)
    dst = ctx.workspace.resolve_output(args.dst, reserved_inputs=inputs)
    list_file = ctx.workspace.resolve_output(
        args.list_path,
        reserved_inputs={*inputs, dst},
    )
    lines = []
    for relative in args.inputs:
        path = ctx.workspace.resolve_input(relative)
        lines.append(f"file '{_concat_escape(str(path))}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, ConcatArgs)
    inputs = _input_paths(args, ctx)
    list_file = ctx.workspace.resolve_input(args.list_path)
    if list_file in inputs:
        raise SandboxError(f"concat 列表文件不能是输入片段: {args.list_path!r}")
    dst = ctx.workspace.resolve_output(
        args.dst,
        reserved_inputs={*inputs, list_file},
    )
    return [
        ctx.settings.ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(dst),
    ]
