"""镜头切分契约（P1 感知层预留）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.time import RationalTime


class ShotBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t: RationalTime
    confidence: float = Field(ge=0.0, le=1.0)
    type: Literal["hard", "fade"] = "hard"


class ShotList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    shots: list[ShotBoundary] = Field(default_factory=list)
