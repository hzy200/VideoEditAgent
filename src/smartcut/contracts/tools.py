"""受控工具调用契约。

AI 永远不产出这里的 argv；只有 Compiler 生成 ToolCall，Runner 负责执行。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ToolName = Literal[
    "probe",
    "cut",
    "concat",
    "compose",
    "verify",
    "extract_audio",
    "proxy",
    "extract_frames",
    "extract_evidence_frames",
    "fit",
    "subtitles",
    "loudnorm",
    "postprocess",
    "mix_audio",
    "transcode",
    "media_repair",
    "loudnorm_measure",
]


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)
    tool: ToolName
    args: dict[str, Any]
    description: str | None = None


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    argv: list[str]
    exit_code: int
    duration_ms: int
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = Field(default_factory=list)
    ok: bool = True
    cached: bool = False
    encoder: str | None = None
