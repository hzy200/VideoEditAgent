"""字幕 cue 生成与 SRT/VTT 格式化。

结构而非算术：输出时间由 trim 顺序推导，不依赖模型给输出时间。
"""

from __future__ import annotations

from fractions import Fraction

from smartcut.contracts.captions import CaptionCue
from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.time import RationalTime
from smartcut.contracts.transcript import Transcript

MAX_CUE_CHARS = 42
MAX_CUE_SECONDS = 6.0
MAX_WORD_GAP = 0.8


def _source_words(transcript: Transcript) -> list[tuple[Fraction, Fraction, str]]:
    words: list[tuple[Fraction, Fraction, str]] = []
    if transcript.words:
        for word in transcript.words:
            text = word.text.strip()
            if text:
                words.append((word.start.as_fraction(), word.end.as_fraction(), text))
        return words
    for segment in transcript.segments:
        text = segment.text.strip()
        if text:
            words.append((segment.start.as_fraction(), segment.end.as_fraction(), text))
    return words


def build_cues(ir: EditIR, transcript: Transcript | None) -> list[CaptionCue]:
    """手动字幕优先；None 时由转写词与 trim 结构自动生成。"""
    if ir.subtitles is not None:
        return list(ir.subtitles)
    if transcript is None or transcript.degraded:
        return []
    source_words = _source_words(transcript)
    if not source_words:
        return []

    transition_after = {
        item.after: item.duration.as_fraction() for item in ir.transitions
    }
    mapped: list[tuple[CaptionCue, int]] = []
    offset = Fraction(0)
    for trim_index, op in enumerate(ir.sequence):
        trim_start = op.range.start.as_fraction()
        trim_end = op.range.end.as_fraction()
        speed = Fraction(str(op.speed))
        output_duration = op.output_duration().as_fraction()
        for word_start, word_end, text in source_words:
            overlap_start = max(word_start, trim_start)
            overlap_end = min(word_end, trim_end)
            if overlap_end <= overlap_start:
                continue
            start = offset + (overlap_start - trim_start) / speed
            end = offset + (overlap_end - trim_start) / speed
            start = max(offset, min(offset + output_duration, start))
            end = max(offset, min(offset + output_duration, end))
            if end <= start:
                continue
            mapped.append(
                (
                    CaptionCue(
                        start=RationalTime.from_fraction(start),
                        end=RationalTime.from_fraction(end),
                        text=text,
                    ),
                    trim_index,
                )
            )
        offset += output_duration - transition_after.get(op.id or "", Fraction(0))

    mapped.sort(key=lambda item: item[0].start.as_fraction())

    cues: list[CaptionCue] = []
    current_text: list[str] = []
    current_start: RationalTime | None = None
    current_end: RationalTime | None = None
    current_trim: int | None = None

    def flush() -> None:
        nonlocal current_text, current_start, current_end, current_trim
        if current_start is not None and current_end is not None and current_text:
            cues.append(
                CaptionCue(
                    start=current_start,
                    end=current_end,
                    text=" ".join(current_text).strip(),
                )
            )
        current_text = []
        current_start = None
        current_end = None
        current_trim = None

    for cue, trim_index in mapped:
        if current_start is None:
            current_start = cue.start
            current_end = cue.end
            current_trim = trim_index
            current_text = [cue.text]
            continue

        assert current_end is not None
        gap = cue.start.to_float() - current_end.to_float()
        duration = cue.end.to_float() - current_start.to_float()
        text_length = len(" ".join(current_text)) + len(cue.text) + 1
        if (
            trim_index != current_trim
            or gap > MAX_WORD_GAP
            or duration > MAX_CUE_SECONDS
            or text_length > MAX_CUE_CHARS
        ):
            flush()
            current_start = cue.start
            current_end = cue.end
            current_trim = trim_index
            current_text = [cue.text]
        else:
            current_end = cue.end
            current_text.append(cue.text)

    flush()
    return cues


def _timestamp(value: RationalTime, *, vtt: bool) -> str:
    total = max(0.0, value.to_float())
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    seconds = total % 60
    separator = "." if vtt else ","
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}".replace(".", separator)


def format_srt(cues: list[CaptionCue]) -> str:
    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            f"{index}\n"
            f"{_timestamp(cue.start, vtt=False)} --> {_timestamp(cue.end, vtt=False)}\n"
            f"{cue.text}\n"
        )
    return "\n".join(blocks)


def format_vtt(cues: list[CaptionCue]) -> str:
    blocks = ["WEBVTT\n"]
    for cue in cues:
        blocks.append(
            f"{_timestamp(cue.start, vtt=True)} --> {_timestamp(cue.end, vtt=True)}\n"
            f"{cue.text}\n"
        )
    return "\n".join(blocks)
