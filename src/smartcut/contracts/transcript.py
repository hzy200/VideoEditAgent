"""转写契约：词级 + 句级时间戳。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from smartcut.contracts.time import RationalTime


class _Spanned(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: RationalTime
    end: RationalTime

    @model_validator(mode="after")
    def _ordered(self) -> "_Spanned":
        if self.end < self.start:
            raise ValueError("end 不能早于 start")
        return self


class Word(_Spanned):
    text: str
    probability: float | None = None


class TranscriptSegment(_Spanned):
    id: int | None = None
    text: str
    words: list[Word] = Field(default_factory=list)


class Transcript(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str | None = None
    words: list[Word] = Field(default_factory=list)
    segments: list[TranscriptSegment] = Field(default_factory=list)
    degraded: bool = False
    note: str | None = None
