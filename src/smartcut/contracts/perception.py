"""感知结果聚合契约。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.chapters import Chapter
from smartcut.contracts.faces import FaceReport
from smartcut.contracts.quality import QualityReport
from smartcut.contracts.shots import ShotList
from smartcut.contracts.time import RationalTime


class FrameRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    t: RationalTime
    path: str
    # 同一次 ffmpeg 解码产出的 contact-sheet 缩略图（可为空）。
    thumbnail: str | None = None


class PerceptionData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    frame_manifest: list[FrameRef] = Field(default_factory=list)
    sampled_fps: float = 1.0
    shots: ShotList | None = None
    quality: QualityReport | None = None
    faces: FaceReport | None = None
    contact_sheet: str | None = None
    chapters: list[Chapter] = Field(default_factory=list)
    available: bool = True
    warnings: list[str] = Field(default_factory=list)
