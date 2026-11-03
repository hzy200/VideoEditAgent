"""章节契约：长视频粗粒度结构。"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from smartcut.contracts.ids import SafeId
from smartcut.contracts.time import RationalTime


class Chapter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: SafeId
    start: RationalTime
    end: RationalTime
    title: str | None = None
    summary: str | None = None
    shot_count: int = Field(default=0, ge=0)
    quality_marks: int = Field(default=0, ge=0)
    face_frames: int = Field(default=0, ge=0)
    keyframes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ordered(self) -> "Chapter":
        if self.end <= self.start:
            raise ValueError("chapter.end 必须大于 start")
        return self


class ChapterPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    chapters: list[Chapter] = Field(default_factory=list)
    rationale: str = ""
    origin: Literal["auto", "approved"] = "auto"


class OutlineApproval(BaseModel):
    """人工确认后的大纲快照；章节顺序即用户确认顺序。"""

    model_config = ConfigDict(extra="forbid")

    project_id: SafeId
    run_id: SafeId
    src: str
    source_hash: str
    goal: str = ""
    chapters: list[Chapter] = Field(min_length=1)
    notes: str = ""

    @field_validator("source_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{16,64}", value):
            raise ValueError("source_hash 必须是 16-64 位十六进制")
        return value

    @model_validator(mode="after")
    def _unique_chapters(self) -> "OutlineApproval":
        ids = [chapter.id for chapter in self.chapters]
        if len(ids) != len(set(ids)):
            raise ValueError("大纲章节 id 不能重复")
        return self
