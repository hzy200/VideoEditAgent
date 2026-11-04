"""证据抽帧：只解码候选窗口，showinfo 输出真实 PTS。

与 ``extract_frames`` 分离，避免改变既有 1fps 帧缓存与 manifest。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from smartcut.contracts.time import RationalTime
from smartcut.runner.tools.base import ToolContext

HwAccel = Literal["cuda", "qsv", "d3d11va", "vaapi"]
PREROLL_SECONDS = Fraction(2)


class EvidenceWindowsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    start: RationalTime
    end: RationalTime
    # 容器起始时间；用于把 showinfo 的绝对 PTS 换回媒体时间。
    source_start: RationalTime = Field(
        default_factory=lambda: RationalTime(value=0, rate=90_000)
    )
    fps: float = Field(default=2.0, gt=0.0, le=30.0)
    width: int = Field(default=640, ge=160, le=1920)
    quality: int = Field(default=3, ge=2, le=31)
    dst_dir: str = "frames/evidence"
    hwaccel: HwAccel | None = None
    # 可视化 P/B 帧运动向量与残差区域；不承诺数值级 MV。
    codecview: bool = False

    @model_validator(mode="after")
    def _ordered(self) -> "EvidenceWindowsArgs":
        if self.end <= self.start:
            raise ValueError("end 必须大于 start")
        return self


def prepare(args: BaseModel, ctx: ToolContext) -> None:
    assert isinstance(args, EvidenceWindowsArgs)
    target = ctx.workspace.resolve(args.dst_dir)
    target.mkdir(parents=True, exist_ok=True)
    for old in target.glob("frame_*.jpg"):
        old.unlink(missing_ok=True)


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, EvidenceWindowsArgs)
    src = ctx.workspace.resolve_input(args.src)
    target = ctx.workspace.resolve(args.dst_dir)
    target.mkdir(parents=True, exist_ok=True)
    pattern = str(target / "frame_%06d.jpg")
    hwaccel = ["-hwaccel", args.hwaccel] if args.hwaccel else []

    start_seconds = args.start.as_fraction()
    end_seconds = args.end.as_fraction()
    origin = args.source_start.as_fraction()
    seek = max(Fraction(0), start_seconds - PREROLL_SECONDS)
    duration = max(Fraction(1, 10), end_seconds - seek)
    trim_start = start_seconds + origin
    trim_end = end_seconds + origin
    filters = (
        f"trim=start={float(trim_start):.6f}:end={float(trim_end):.6f},"
        f"setpts=PTS-STARTPTS+{float(trim_start):.6f}/TB,"
        f"fps={args.fps},scale={args.width}:-2,showinfo"
    )
    if args.codecview:
        filters += ",codecview=mv=pf+bf+bb"
    return [
        ctx.settings.ffmpeg_bin,
        "-hide_banner",
        "-y",
        *hwaccel,
        "-copyts",
        "-ss",
        f"{float(seek):.6f}",
        "-i",
        str(src),
        "-t",
        f"{float(duration):.6f}",
        "-an",
        "-vf",
        filters,
        "-q:v",
        str(args.quality),
        "-start_number",
        "0",
        pattern,
    ]
