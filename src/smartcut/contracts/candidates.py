"""候选片段契约：供检索与 Planner 选择。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.edit_ir import TimeRange
from smartcut.contracts.ids import SafeId


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: SafeId
    source_range: TimeRange
    text: str = ""
    score: float = Field(default=0.5, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)
    face_present: bool = False
    shot_boundary: bool = False
    # 64-bit dHash（Pillow 不可用时为 None）；用于“找相似片段”，不参与语义理解。
    visual_hash: int | None = Field(default=None, ge=0)
    keyframe: str | None = None




class CandidateList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    candidates: list[Candidate] = Field(default_factory=list)
