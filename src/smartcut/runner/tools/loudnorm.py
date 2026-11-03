"""响度归一：单遍 FFmpeg loudnorm，默认 -14 LUFS。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.runner.tools.base import ToolContext


class LoudnormArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    target_lufs: float = Field(default=-14.0, ge=-70.0, le=0.0)
    true_peak: float = Field(default=-1.5, ge=-9.0, le=0.0)
    lra: float = Field(default=11.0, ge=1.0, le=50.0)
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, LoudnormArgs)
    src = ctx.workspace.resolve_input(args.src)
    dst = ctx.workspace.resolve_output(args.dst, reserved_inputs={src})
    return [
        ctx.settings.ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "copy",
        "-af",
        f"loudnorm=I={args.target_lufs}:TP={args.true_peak}:LRA={args.lra}",
        "-c:a",
        args.audio_codec,
        "-b:a",
        args.audio_bitrate,
        "-movflags",
        "+faststart",
        str(dst),
    ]
