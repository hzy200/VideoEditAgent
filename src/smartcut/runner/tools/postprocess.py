"""单次后处理：调色/裁剪/去水印/水印/字幕/响度合并为一次 ffmpeg 调用。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from smartcut.contracts.encoder import EncoderProfile, video_encoder_args
from smartcut.contracts.time import RationalTime
from smartcut.runner.tools.audio_filters import audio_filter_chain
from smartcut.runner.tools.base import ToolContext
from smartcut.runner.tools.filters import validate_video_filters

_RATE = 90_000


def _zero() -> RationalTime:
    return RationalTime(value=0, rate=_RATE)


class OverlayInput(BaseModel):
    """Compiler 解析后的水印参数；图片水印带工作区相对 src。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["text", "image"] = "text"
    text: str | None = None
    src: str | None = None
    width: int | None = Field(default=None, ge=2)
    position: Literal[
        "top_left",
        "top_right",
        "bottom_left",
        "bottom_right",
        "center",
    ] = "bottom_right"
    font_size: int = Field(default=28, ge=8, le=120)
    color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    opacity: float = Field(default=0.7, ge=0.0, le=1.0)
    start: RationalTime = Field(default_factory=_zero)
    end: RationalTime | None = None

    @model_validator(mode="after")
    def _shape(self) -> "OverlayInput":
        if self.kind == "text":
            if not (self.text or "").strip():
                raise ValueError("文字水印必须提供 text")
            if self.src is not None or self.width is not None:
                raise ValueError("文字水印不得携带 src/width")
        else:
            if not self.src:
                raise ValueError("图片水印必须提供 src")
            if self.width is None:
                raise ValueError("图片水印必须提供 width")
            if self.text:
                raise ValueError("图片水印不得携带 text")
        return self


class RemovalInput(BaseModel):
    """源画面矩形去水印区域（输出坐标）。"""

    model_config = ConfigDict(extra="forbid")

    x: int = Field(ge=1)
    y: int = Field(ge=1)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    start: RationalTime = Field(default_factory=_zero)
    end: RationalTime | None = None


class PostProcessArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    crop_x: int | None = Field(default=None, ge=0)
    crop_y: int | None = Field(default=None, ge=0)
    crop_width: int | None = Field(default=None, gt=0)
    crop_height: int | None = Field(default=None, gt=0)
    target_width: int = Field(default=1080, ge=2)
    target_height: int = Field(default=1920, ge=2)
    srt: str | None = None
    font_name: str = "Microsoft YaHei"
    font_size: int = Field(default=22, ge=8, le=96)
    primary_colour: str = "&H00FFFFFF"
    alignment: int = Field(default=2, ge=1, le=9)
    outline: int = Field(default=2, ge=0, le=8)
    fonts_dir: str | None = None
    target_lufs: float | None = Field(default=None, ge=-70.0, le=0.0)
    true_peak: float = Field(default=-1.5, ge=-9.0, le=0.0)
    lra: float = Field(default=11.0, ge=1.0, le=50.0)
    volume_db: float = Field(default=0.0, ge=-60.0, le=12.0, allow_inf_nan=False)
    pitch_semitones: float = Field(
        default=0.0, ge=-12.0, le=12.0, allow_inf_nan=False
    )
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = Field(default=18, ge=0, le=51)
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    video_track_timescale: int = Field(default=90_000, gt=0)
    fps_mode: Literal["cfr", "vfr", "passthrough"] = "passthrough"
    hdr_tonemap: bool = False
    loudnorm_measured: dict[str, str] | None = None
    encoder: EncoderProfile | None = None
    # Compiler 生成的白名单滤镜；pre 在 fit/scale 之前，post 在其之后。
    video_filters_pre: list[str] = Field(default_factory=list)
    video_filters_post: list[str] = Field(default_factory=list)
    overlays: list[OverlayInput] = Field(default_factory=list)
    removals: list[RemovalInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def _crop_complete(self) -> "PostProcessArgs":
        values = (self.crop_x, self.crop_y, self.crop_width, self.crop_height)
        if any(item is not None for item in values) and not all(
            item is not None for item in values
        ):
            raise ValueError("crop_x/y/width/height 必须同时提供")
        for name in ("target_width", "target_height"):
            if getattr(self, name) % 2 != 0:
                raise ValueError(f"{name} 必须为偶数")
        validate_video_filters(self.video_filters_pre)
        validate_video_filters(self.video_filters_post)
        return self

    @property
    def has_crop(self) -> bool:
        return self.crop_width is not None

    @property
    def has_image_overlays(self) -> bool:
        return any(overlay.kind == "image" for overlay in self.overlays)


def _enable(start: RationalTime, end: RationalTime | None) -> str:
    if end is not None:
        return (
            f"enable='between(t,{start.to_ffmpeg_seconds()},"
            f"{end.to_ffmpeg_seconds()})'"
        )
    if start.as_fraction() > 0:
        return f"enable='gte(t,{start.to_ffmpeg_seconds()})'"
    return ""


def _drawtext_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace(":", "\\:")
        .replace("%", "\\%")
        .replace(",", "\\,")
    )


def _text_overlay_filters(overlays: list[OverlayInput]) -> list[str]:
    filters: list[str] = []
    for overlay in overlays:
        if overlay.kind != "text":
            continue
        colour = "0x" + overlay.color.lstrip("#").upper()
        x, y = {
            "top_left": ("12", "12"),
            "top_right": ("w-tw-12", "12"),
            "bottom_left": ("12", "h-th-12"),
            "bottom_right": ("w-tw-12", "h-th-12"),
            "center": ("(w-tw)/2", "(h-th)/2"),
        }[overlay.position]
        parts = [
            f"text='{_drawtext_escape(overlay.text or '')}'",
            "font='Microsoft YaHei'",
            f"fontsize={overlay.font_size}",
            f"fontcolor={colour}@{overlay.opacity:.3f}",
            f"x={x}",
            f"y={y}",
            "borderw=1",
            "bordercolor=black@0.5",
        ]
        window = _enable(overlay.start, overlay.end)
        if window:
            parts.append(window)
        filters.append("drawtext=" + ":".join(parts))
    return filters


def _removal_filters(removals: list[RemovalInput]) -> list[str]:
    filters: list[str] = []
    for removal in removals:
        parts = [
            f"x={removal.x}",
            f"y={removal.y}",
            f"w={removal.width}",
            f"h={removal.height}",
            "show=0",
        ]
        window = _enable(removal.start, removal.end)
        if window:
            parts.append(window)
        filters.append("delogo=" + ":".join(parts))
    return filters


def _subtitle_filter(args: PostProcessArgs, ctx: ToolContext) -> str | None:
    if not args.srt:
        return None
    srt = ctx.workspace.resolve_input(args.srt)
    relative_srt = srt.relative_to(ctx.workspace.root).as_posix()
    style = (
        f"FontName={args.font_name},FontSize={args.font_size},"
        f"PrimaryColour={args.primary_colour},OutlineColour=&H00000000,"
        f"BorderStyle=1,Outline={args.outline},Alignment={args.alignment}"
    )
    fonts = ""
    if args.fonts_dir:
        fonts_path = ctx.workspace.resolve_input(args.fonts_dir)
        fonts = f":fontsdir={fonts_path.relative_to(ctx.workspace.root).as_posix()}"
    if srt.suffix.lower() in {".ass", ".ssa"}:
        return f"ass={relative_srt}{fonts}"
    return f"subtitles={relative_srt}{fonts}:force_style='{style}'"


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, PostProcessArgs)
    src = ctx.workspace.resolve_input(args.src)
    image_overlays = [
        overlay for overlay in args.overlays if overlay.kind == "image"
    ]
    image_paths = [
        ctx.workspace.resolve_input(overlay.src or "")
        for overlay in image_overlays
    ]
    dst = ctx.workspace.resolve_output(
        args.dst, reserved_inputs={src, *image_paths}
    )

    filters: list[str] = []
    if args.hdr_tonemap:
        filters.extend(
            [
                "zscale=t=linear:npl=100",
                "format=gbrpf32le",
                "zscale=p=bt709",
                "tonemap=tonemap=hable:desat=0",
                "zscale=t=bt709:m=bt709:r=tv",
                "format=yuv420p",
            ]
        )
    filters.extend(args.video_filters_pre)
    if args.has_crop:
        filters.append(
            f"crop={args.crop_width}:{args.crop_height}:{args.crop_x}:{args.crop_y}"
        )
        filters.append(f"scale={args.target_width}:{args.target_height}")
        filters.append("setsar=1")
    filters.extend(args.video_filters_post)
    filters.extend(_removal_filters(args.removals))
    filters.extend(_text_overlay_filters(args.overlays))
    subtitle = _subtitle_filter(args, ctx)

    if args.has_image_overlays:
        argv = [ctx.settings.ffmpeg_bin, "-hide_banner", "-y", "-i", str(src)]
        for path in image_paths:
            argv.extend(["-i", str(path)])

        graph: list[str] = []
        base_chain = ",".join(filters) if filters else "null"
        graph.append(f"[0:v]{base_chain}[vbase]")
        current = "vbase"
        for offset, overlay in enumerate(image_overlays):
            x, y = {
                "top_left": ("12", "12"),
                "top_right": ("W-w-12", "12"),
                "bottom_left": ("12", "H-h-12"),
                "bottom_right": ("W-w-12", "H-h-12"),
                "center": ("(W-w)/2", "(H-h)/2"),
            }[overlay.position]
            input_index = offset + 1
            graph.append(
                f"[{input_index}:v]scale={overlay.width}:-2,format=rgba,"
                f"colorchannelmixer=aa={overlay.opacity:.3f}[wm{offset}]"
            )
            window = _enable(overlay.start, overlay.end)
            suffix = f":{window}" if window else ""
            next_label = f"vimg{offset}"
            graph.append(
                f"[{current}][wm{offset}]overlay=x={x}:y={y}:"
                f"eof_action=repeat{suffix}[{next_label}]"
            )
            current = next_label
        if subtitle:
            graph.append(f"[{current}]{subtitle}[vout]")
            current = "vout"
        argv.extend(
            ["-filter_complex", ";".join(graph), "-map", f"[{current}]"]
        )
        argv.extend(["-map", "0:a:0?"])
        if args.fps_mode != "passthrough":
            argv.extend(["-fps_mode", args.fps_mode])
        encoder_args = (
            video_encoder_args(args.encoder)
            if args.encoder is not None
            else [
                "-c:v",
                args.video_codec,
                "-preset",
                args.preset,
                "-crf",
                str(args.crf),
            ]
        )
        argv.extend(encoder_args)
        argv.extend(
            [
                "-pix_fmt",
                "yuv420p",
                "-video_track_timescale",
                str(args.video_track_timescale),
            ]
        )
    else:
        if subtitle:
            filters.append(subtitle)
        argv = [
            ctx.settings.ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
        ]
        if filters:
            argv.extend(["-vf", ",".join(filters)])
            if args.fps_mode != "passthrough":
                argv.extend(["-fps_mode", args.fps_mode])
            encoder_args = (
                video_encoder_args(args.encoder)
                if args.encoder is not None
                else [
                    "-c:v",
                    args.video_codec,
                    "-preset",
                    args.preset,
                    "-crf",
                    str(args.crf),
                ]
            )
            argv.extend(encoder_args)
            argv.extend(
                [
                    "-pix_fmt",
                    "yuv420p",
                    "-video_track_timescale",
                    str(args.video_track_timescale),
                ]
            )
        else:
            argv.extend(["-c:v", "copy"])

    audio_filters = audio_filter_chain(
        volume_db=args.volume_db,
        pitch_semitones=args.pitch_semitones,
        target_lufs=args.target_lufs,
        true_peak=args.true_peak,
        lra=args.lra,
        measured=args.loudnorm_measured,
    )

    if audio_filters:
        argv.extend(
            [
                "-af",
                ",".join(audio_filters),
                "-c:a",
                args.audio_codec,
                "-b:a",
                args.audio_bitrate,
            ]
        )
    else:
        argv.extend(["-c:a", "copy"])

    argv.extend(["-movflags", "+faststart", str(dst)])
    return argv
