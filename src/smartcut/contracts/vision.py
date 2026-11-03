"""视觉摘要契约：VLM 输出章节级视觉说明，供文本 Planner 使用。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.ids import SafeId


class ChapterVisualNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: SafeId
    summary: str = ""
    visual_tags: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)


class ImageVisualNote(BaseModel):
    """单张图片的描述，可按图片 sha256 单独缓存。"""

    model_config = ConfigDict(extra="forbid")

    image_index: int = Field(ge=0)
    summary: str = ""
    visual_tags: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)


class ImageVisualNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    images: list[ImageVisualNote] = Field(default_factory=list)


class VisualNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapters: list[ChapterVisualNote] = Field(default_factory=list)
    overall: str = ""
    model: str | None = None
    image_count: int = Field(default=0, ge=0)
