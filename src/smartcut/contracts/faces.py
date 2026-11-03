"""人脸锚点契约。

坐标使用归一化值（0-1），与抽帧分辨率无关；Compiler 再映射到源尺寸。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from smartcut.contracts.time import RationalTime


class FaceAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t: RationalTime
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _inside_frame(self) -> "FaceAnchor":
        if self.x + self.w > 1.001 or self.y + self.h > 1.001:
            raise ValueError("face bbox 必须落在画面内")
        return self

    @property
    def center_x(self) -> float:
        return min(1.0, max(0.0, self.x + self.w / 2))

    @property
    def center_y(self) -> float:
        return min(1.0, max(0.0, self.y + self.h / 2))


class FaceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    frame_width: int = 0
    frame_height: int = 0
    sampled_fps: float = 1.0
    anchors: list[FaceAnchor] = Field(default_factory=list)
    # thread / process / thread_fallback
    engine: str = "thread"

    @property
    def has_faces(self) -> bool:
        return bool(self.anchors)
