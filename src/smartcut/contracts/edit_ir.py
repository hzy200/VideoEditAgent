"""EditIR：AI 与系统之间唯一的编辑决策契约。

硬规则：
1. 结构而非算术：顺序推导输出时间，模型不做时长减法。
2. effects 不得改变时长；speed/transition 属于时间轴词汇，不属于 effect。
3. 有理数时间；禁止浮点秒。
4. project_id 只允许安全字符，防止路径注入。
"""

from __future__ import annotations

import hashlib
from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from smartcut.contracts.audio import AudioTrackSpec
from smartcut.contracts.captions import CaptionCue
from smartcut.contracts.effects import Effect, EffectSpec
from smartcut.contracts.ids import SafeId
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.time import RationalTime
from smartcut.contracts.timeline_ops import (
    OverlaySpec,
    TransitionSpec,
    WatermarkRemovalSpec,
)


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: RationalTime
    end: RationalTime

    @model_validator(mode="after")
    def _ordered(self) -> "TimeRange":
        if self.end <= self.start:
            raise ValueError("range.end 必须大于 range.start")
        return self

    def duration(self) -> RationalTime:
        return self.end - self.start


class TrimOp(BaseModel):
    """从同一素材保留一个源区间。源时间由用户/模型给出，输出时间由编译器推导。

    ``id`` 用于逐片段特效的稳定引用；旧 IR 可以没有 id，由服务层在保存新 revision 时分配。
    """

    model_config = ConfigDict(extra="forbid")

    op: Literal["trim"] = "trim"
    id: SafeId | None = None
    src: str
    range: TimeRange
    # 1.0=原速；0.5=半速；2.0=两倍速。改变输出时长，因此属于时间轴算子。
    speed: float = Field(default=1.0, ge=0.5, le=2.0, allow_inf_nan=False)
    tag: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # 规则/LLM 给出的短理由；仅供解释与审片展示，不参与编译。
    rationale: str | None = Field(default=None, max_length=120)

    def output_duration(self) -> RationalTime:
        return RationalTime.from_fraction(
            self.range.duration().as_fraction() / Fraction(str(self.speed))
        )


def trim_id_for(src: str, time_range: TimeRange) -> str:
    """由源区间内容派生稳定 trim id，供 GUI 与 Planner 共用。"""
    digest = hashlib.sha1(
        (
            f"{src}|{time_range.start.value}/{time_range.start.rate}|"
            f"{time_range.end.value}/{time_range.end.rate}"
        ).encode("utf-8")
    ).hexdigest()[:10]
    return f"seg_{digest}"


def assign_trim_ids(sequence: list[TrimOp]) -> list[TrimOp]:
    """为缺失 id 的 trim 生成稳定 id；已有 id 原样保留。

    id 由源区间内容派生，避免重排导致引用错位；同一 IR 重复保存得到相同 id。
    """
    used = {op.id for op in sequence if op.id}
    result: list[TrimOp] = []
    for op in sequence:
        if op.id:
            result.append(op)
            continue
        base = trim_id_for(op.src, op.range)
        candidate = base
        suffix = 1
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        result.append(op.model_copy(update={"id": candidate}))
    return result


class AudioSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loudness: str | None = None
    remove_filler: bool = False
    bgm: str | None = None
    # 全局音频调整；在单次 postprocess 中应用，pitch 保持时长不变。
    volume_db: float = Field(default=0.0, ge=-60.0, le=12.0, allow_inf_nan=False)
    pitch_semitones: float = Field(default=0.0, ge=-12.0, le=12.0, allow_inf_nan=False)
    # None=旧单音轨行为；[]=明确静音；非空=由 mix_audio 统一混音。
    tracks: list[AudioTrackSpec] | None = None

    @model_validator(mode="after")
    def _unique_tracks(self) -> "AudioSpec":
        if self.tracks is None:
            return self
        ids = [track.id for track in self.tracks]
        if len(ids) != len(set(ids)):
            raise ValueError("音轨 id 必须唯一")
        return self


class ExportSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: Literal["mp4_h264", "reels", "douyin", "youtube", "custom"] = "mp4_h264"
    burn_captions: bool = False
    cut_mode: Literal["reencode", "copy"] = "reencode"
    fps: str | None = None


class EditIR(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["editir/v1"] = Field(default="editir/v1", alias="schema")
    project_id: SafeId
    revision: int = Field(default=0, ge=0)
    media_facts: MediaFacts
    sequence: list[TrimOp] = Field(min_length=1)
    effects: list[Effect] = Field(default_factory=list)
    transitions: list[TransitionSpec] = Field(default_factory=list)
    overlays: list[OverlaySpec] = Field(default_factory=list)
    removals: list[WatermarkRemovalSpec] = Field(default_factory=list)
    audio: AudioSpec = Field(default_factory=AudioSpec)
    # 手动字幕使用输出时间线；None=自动生成，[]=明确无字幕，非空=手动覆盖。
    subtitles: list[CaptionCue] | None = None
    export: ExportSpec = Field(default_factory=ExportSpec)

    @model_validator(mode="after")
    def _single_source(self) -> "EditIR":
        for index, op in enumerate(self.sequence):
            if op.src != self.media_facts.src:
                raise ValueError(
                    f"sequence[{index}].src={op.src!r} 与 media_facts.src={self.media_facts.src!r} 不一致"
                )
        ids = [op.id for op in self.sequence if op.id]
        if len(ids) != len(set(ids)):
            raise ValueError("sequence 内的 trim id 必须唯一")
        return self

    def total_duration(self) -> RationalTime:
        # 统一时基 90000；时间轴运算本身仍走 Fraction。
        total = sum(
            (op.output_duration().as_fraction() for op in self.sequence),
            Fraction(0),
        )
        overlap = sum(
            (item.duration.as_fraction() for item in self.transitions),
            Fraction(0),
        )
        return RationalTime.from_fraction(total - overlap).round_to_rate(90_000)

    def dump_ir(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)
