"""视觉摘要契约：VLM 输出章节级视觉说明，供文本 Planner 使用。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from smartcut.contracts.ids import SafeId
from smartcut.contracts.visual_evidence import LABEL_RE


def _validate_refs(value: list[str]) -> list[str]:
    if len(value) > 8:
        raise ValueError("evidence_refs 最多 8 个")
    if len(value) != len(set(value)):
        raise ValueError("evidence_refs 不能重复")
    for item in value:
        if not LABEL_RE.fullmatch(item):
            raise ValueError(f"非法证据标签: {item!r}")
    return value


class ChapterVisualNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: SafeId
    summary: str = ""
    visual_tags: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("evidence_refs")
    @classmethod
    def _refs(cls, value: list[str]) -> list[str]:
        return _validate_refs(value)


class ImageVisualNote(BaseModel):
    """单张图片的描述，可按图片 sha256 单独缓存。"""

    model_config = ConfigDict(extra="forbid")

    image_index: int = Field(ge=0)
    summary: str = ""
    visual_tags: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("evidence_refs")
    @classmethod
    def _refs(cls, value: list[str]) -> list[str]:
        return _validate_refs(value)


class ImageVisualNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    images: list[ImageVisualNote] = Field(default_factory=list)


class VisualNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapters: list[ChapterVisualNote] = Field(default_factory=list)
    overall: str = ""
    model: str | None = None
    image_count: int = Field(default=0, ge=0)
    evidence_ids: list[str] = Field(default_factory=list)
