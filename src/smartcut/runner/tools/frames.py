"""抽帧工具：1fps 采样 JPEG，供 OpenCV 质量/人脸/contact sheet 使用。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from smartcut.runner.tools.base import ToolContext

HwAccel = Literal["cuda", "qsv", "d3d11va", "vaapi"]


class ExtractFramesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    dst_dir: str = "frames/extracted"
    fps: float = Field(default=1.0, gt=0.0, le=30.0)
    width: int = Field(default=640, ge=160, le=1920)
    quality: int = Field(default=3, ge=2, le=31)
    # 可选：同一次解码额外输出 contact-sheet 缩略图，避免重复读大图/缩放。
    thumbs_dir: str | None = None
    thumb_width: int = Field(default=320, ge=80, le=1280)
    thumb_height: int = Field(default=180, ge=45, le=720)
    thumb_quality: int = Field(default=5, ge=2, le=31)
    hwaccel: HwAccel | None = None


def prepare(args: BaseModel, ctx: ToolContext) -> None:
    assert isinstance(args, ExtractFramesArgs)
    target = ctx.workspace.resolve(args.dst_dir)
    target.mkdir(parents=True, exist_ok=True)
    for old in target.glob("frame_*.jpg"):
        old.unlink(missing_ok=True)
    if args.thumbs_dir:
        thumbs = ctx.workspace.resolve(args.thumbs_dir)
        thumbs.mkdir(parents=True, exist_ok=True)
        for old in thumbs.glob("thumb_*.jpg"):
            old.unlink(missing_ok=True)


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, ExtractFramesArgs)
    src = ctx.workspace.resolve_input(args.src)
    target = ctx.workspace.resolve(args.dst_dir)
    target.mkdir(parents=True, exist_ok=True)
    pattern = str(target / "frame_%06d.jpg")
    hwaccel = ["-hwaccel", args.hwaccel] if args.hwaccel else []

    if not args.thumbs_dir:
        return [
            ctx.settings.ffmpeg_bin,
            "-hide_banner",
            "-y",
            *hwaccel,
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-vf",
            f"fps={args.fps},scale={args.width}:-2",
            "-q:v",
            str(args.quality),
            "-start_number",
            "0",
            pattern,
        ]

    thumbs = ctx.workspace.resolve(args.thumbs_dir)
    thumbs.mkdir(parents=True, exist_ok=True)
    thumb_pattern = str(thumbs / "thumb_%06d.jpg")
    # 一次解码，split 后分别输出分析帧与 contact-sheet 缩略图。
    filters = (
        f"[0:v]fps={args.fps},split=2[frames][thumbs];"
        f"[frames]scale={args.width}:-2[out_frames];"
        f"[thumbs]scale={args.thumb_width}:{args.thumb_height}:"
        f"force_original_aspect_ratio=increase,"
        f"crop={args.thumb_width}:{args.thumb_height}[out_thumbs]"
    )
    return [
        ctx.settings.ffmpeg_bin,
        "-hide_banner",
        "-y",
        *hwaccel,
        "-i",
        str(src),
        "-filter_complex",
        filters,
        "-map",
        "[out_frames]",
        "-q:v",
        str(args.quality),
        "-start_number",
        "0",
        pattern,
        "-map",
        "[out_thumbs]",
        "-q:v",
        str(args.thumb_quality),
        "-start_number",
        "0",
        thumb_pattern,
    ]
