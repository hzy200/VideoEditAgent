"""分层记忆索引：章节 → 镜头 → 转写/候选计数的结构化视图。

定位：
- 只引用阶段产物与缓存键，不复制大段文本或图片；
- 按 ``source_hash`` 分区，素材变化自动失效；
- Planner 先读顶层摘要，需要细节时按 chapter_id 展开（见
  ``planner.context.memory_payload``）。

本模块只定义数据形状，构建由 ``perception.memory`` 的纯函数完成。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.time import RationalTime

MEMORY_VERSION = 1


class MemoryChapter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    start: RationalTime
    end: RationalTime
    title: str = ""
    summary: str | None = None
    shot_count: int = Field(default=0, ge=0)
    quality_marks: int = Field(default=0, ge=0)
    face_frames: int = Field(default=0, ge=0)
    candidate_count: int = Field(default=0, ge=0)
    keyframe_count: int = Field(default=0, ge=0)
    has_visual_note: bool = False


class MemoryShot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    chapter_id: str | None = None
    t: RationalTime
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class VideoMemoryIndex(BaseModel):
    """单次 source_hash 下的分层索引根节点。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = MEMORY_VERSION
    source_hash: str
    src: str
    duration: RationalTime
    chapters: list[MemoryChapter] = Field(default_factory=list)
    shots: list[MemoryShot] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    stages: dict[str, bool] = Field(default_factory=dict)
    cache_keys: dict[str, str] = Field(default_factory=dict)
