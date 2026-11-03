"""两遍 loudnorm 的第一遍：只测量，不产出文件。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.runner.tools.base import ToolContext


class LoudnormMeasureArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    target_lufs: float = Field(ge=-70.0, le=0.0)
    true_peak: float = Field(default=-1.5, ge=-9.0, le=0.0)
    lra: float = Field(default=11.0, ge=1.0, le=50.0)


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, LoudnormMeasureArgs)
    src = ctx.workspace.resolve_input(args.src)
    return [
        ctx.settings.ffmpeg_bin,
        "-hide_banner",
        "-nostats",
        "-i",
        str(src),
        "-vn",
        "-af",
        (
            f"loudnorm=I={args.target_lufs}:TP={args.true_peak}:"
            f"LRA={args.lra}:print_format=json"
        ),
        "-f",
        "null",
        "-",
    ]


__all__ = ["LoudnormMeasureArgs", "build_argv"]
