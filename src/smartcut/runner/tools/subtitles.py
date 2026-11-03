"""字幕烧录：使用工作区相对路径，避免 Windows 盘符转义问题。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.encoder import EncoderProfile, video_encoder_args
from smartcut.runner.tools.base import ToolContext


class SubtitlesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    srt: str
    font_name: str = "Microsoft YaHei"
    font_size: int = Field(default=22, ge=8, le=96)
    primary_colour: str = "&H00FFFFFF"
    alignment: int = Field(default=2, ge=1, le=9)
    outline: int = Field(default=2, ge=0, le=8)
    fonts_dir: str | None = None
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = Field(default=18, ge=0, le=51)
    video_track_timescale: int = Field(default=90_000, gt=0)
    encoder: EncoderProfile | None = None


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, SubtitlesArgs)
    src = ctx.workspace.resolve_input(args.src)
    dst = ctx.workspace.resolve_output(args.dst, reserved_inputs={src})
    srt = ctx.workspace.resolve_input(args.srt)
    relative_srt = srt.relative_to(ctx.workspace.root).as_posix()
    style = (
        f"FontName={args.font_name},FontSize={args.font_size},"
        f"PrimaryColour={args.primary_colour},OutlineColour=&H00000000,"
        f"BorderStyle=1,Outline={args.outline},Alignment={args.alignment}"
    )
    suffix = srt.suffix.lower()
    fonts = ""
    if args.fonts_dir:
        fonts_path = ctx.workspace.resolve_input(args.fonts_dir)
        fonts = f":fontsdir={fonts_path.relative_to(ctx.workspace.root).as_posix()}"
    if suffix in {".ass", ".ssa"}:
        video_filter = f"ass={relative_srt}{fonts}"
    else:
        video_filter = f"subtitles={relative_srt}{fonts}:force_style='{style}'"
    encoder_args = (
        video_encoder_args(args.encoder)
        if args.encoder is not None
        else ["-c:v", args.video_codec, "-preset", args.preset, "-crf", str(args.crf)]
    )
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
        "-vf",
        video_filter,
        *encoder_args,
        "-pix_fmt",
        "yuv420p",
        "-video_track_timescale",
        str(args.video_track_timescale),
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(dst),
    ]
