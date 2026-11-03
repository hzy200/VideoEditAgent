"""帧质量标记契约。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.time import RationalTime


class QualityKind(StrEnum):
    black = "black"
    frozen = "frozen"
    blurry = "blurry"
    duplicate = "duplicate"


class QualityMark(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t: RationalTime
    kind: QualityKind
    score: float = Field(ge=0.0, le=1.0)
    detail: str | None = None


class QualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    frame_count: int = 0
    sampled_fps: float = 1.0
    marks: list[QualityMark] = Field(default_factory=list)
    # thread / process / thread_fallback
    engine: str = "thread"
