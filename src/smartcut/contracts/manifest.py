"""视频理解结构化清单：只引用阶段产物，不复制大块内容。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.chapters import ChapterPlan
from smartcut.contracts.faces import FaceReport
from smartcut.contracts.quality import QualityReport
from smartcut.contracts.shots import ShotList
from smartcut.contracts.time import RationalTime
from smartcut.contracts.vision import ChapterVisualNote

MANIFEST_VERSION = 1


class TranscriptDigest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool = False
    degraded: bool = False
    segment_count: int = Field(default=0, ge=0)
    word_count: int = Field(default=0, ge=0)
    first_seconds: float | None = None
    last_seconds: float | None = None
    chunk_keys: list[str] = Field(default_factory=list)
    note: str | None = None


class CandidateDigest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(default=0, ge=0)
    top_tags: list[str] = Field(default_factory=list)


class VideoManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = MANIFEST_VERSION
    source_hash: str
    src: str
    duration: RationalTime
    shots: ShotList | None = None
    chapters: ChapterPlan | None = None
    transcript: TranscriptDigest = Field(default_factory=TranscriptDigest)
    quality: QualityReport | None = None
    faces: FaceReport | None = None
    candidates: CandidateDigest = Field(default_factory=CandidateDigest)
    visual_notes: list[ChapterVisualNote] = Field(default_factory=list)
    cache_keys: dict[str, str] = Field(default_factory=dict)
    created_at: str | None = None
