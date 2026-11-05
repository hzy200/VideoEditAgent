"""屏幕文字证据：只作章节/规划候选事实，不写 IR 或时间线。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from smartcut.contracts.time import RationalTime

OCR_VERSION = "ocr@1.0"


class ScreenTextEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t: RationalTime
    text: str = Field(min_length=1, max_length=200)
    bbox_norm: tuple[float, float, float, float]
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    stability: float = Field(default=0.0, ge=0.0, le=1.0)
    sha256: str = Field(min_length=16, max_length=64)

    @field_validator("text")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("屏幕文字不能为空")
        return cleaned

    @model_validator(mode="after")
    def _bbox_inside(self) -> "ScreenTextEvidence":
        x, y, w, h = self.bbox_norm
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and 0.0 < w <= 1.0 and 0.0 < h <= 1.0):
            raise ValueError("bbox_norm 必须在 0-1 内")
        if x + w > 1.001 or y + h > 1.001:
            raise ValueError("bbox_norm 必须落在画面内")
        return self


class ScreenTextReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    engine: str = "unavailable"
    version: str = OCR_VERSION
    items: list[ScreenTextEvidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    available: bool = False
