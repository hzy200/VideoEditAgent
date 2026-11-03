from __future__ import annotations

from smartcut.compiler.captions import build_cues, format_srt, format_vtt
from smartcut.contracts.captions import CaptionCue
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.contracts.transcript import Transcript, TranscriptSegment, Word


def _facts() -> MediaFacts:
    return MediaFacts(
        src="media/source.mp4",
        duration=RationalTime.from_seconds("10", rate=90_000),
        fps="30/1",
        video=StreamFacts(codec_type="video", width=320, height=240),
        audio=StreamFacts(codec_type="audio", channels=2),
    )


def _trim(start: str, end: str) -> TrimOp:
    return TrimOp(
        src="media/source.mp4",
        range=TimeRange(
            start=RationalTime.from_seconds(start, rate=90_000),
            end=RationalTime.from_seconds(end, rate=90_000),
        ),
    )


def _transcript() -> Transcript:
    words = [
        Word(
            text="hello",
            start=RationalTime.from_seconds("0.2", rate=90_000),
            end=RationalTime.from_seconds("0.5", rate=90_000),
        ),
        Word(
            text="world",
            start=RationalTime.from_seconds("0.6", rate=90_000),
            end=RationalTime.from_seconds("0.9", rate=90_000),
        ),
        Word(
            text="second",
            start=RationalTime.from_seconds("2.2", rate=90_000),
            end=RationalTime.from_seconds("2.6", rate=90_000),
        ),
    ]
    segments = [
        TranscriptSegment(
            id=0,
            text="hello world",
            start=words[0].start,
            end=words[1].end,
            words=words[:2],
        ),
        TranscriptSegment(
            id=1,
            text="second",
            start=words[2].start,
            end=words[2].end,
            words=words[2:],
        ),
    ]
    return Transcript(language="en", words=words, segments=segments, degraded=False)


def test_cues_are_remapped_by_trim_structure() -> None:
    ir = EditIR(
        project_id="p_test",
        media_facts=_facts(),
        sequence=[_trim("0", "1"), _trim("2", "3")],
    )
    cues = build_cues(ir, _transcript())

    assert len(cues) == 2
    assert cues[0].text == "hello world"
    assert cues[0].start.to_ffmpeg_seconds() == "0.2"
    assert cues[0].end.to_ffmpeg_seconds() == "0.9"
    assert cues[1].text == "second"
    assert cues[1].start.to_ffmpeg_seconds() == "1.2"
    assert cues[1].end.to_ffmpeg_seconds() == "1.6"


def test_manual_subtitles_override_transcript_and_work_without_it() -> None:
    manual = [
        CaptionCue(
            start=RationalTime.from_seconds("0.5", rate=90_000),
            end=RationalTime.from_seconds("1.5", rate=90_000),
            text="手动字幕",
        )
    ]
    ir = EditIR(
        project_id="p_test",
        media_facts=_facts(),
        sequence=[_trim("0", "1")],
        subtitles=manual,
    )
    assert [cue.text for cue in build_cues(ir, _transcript())] == ["手动字幕"]
    assert [cue.text for cue in build_cues(ir, None)] == ["手动字幕"]

    empty = EditIR(
        project_id="p_test",
        media_facts=_facts(),
        sequence=[_trim("0", "1")],
        subtitles=[],
    )
    assert build_cues(empty, _transcript()) == []


def test_srt_and_vtt_formatting() -> None:
    ir = EditIR(
        project_id="p_test",
        media_facts=_facts(),
        sequence=[_trim("0", "1")],
    )
    cues = build_cues(ir, _transcript())
    srt = format_srt(cues)
    vtt = format_vtt(cues)

    assert "1\n00:00:00,200 --> 00:00:00,900" in srt
    assert "hello world" in srt
    assert vtt.startswith("WEBVTT")
    assert "00:00:00.200 --> 00:00:00.900" in vtt


def test_degraded_transcript_yields_no_cues() -> None:
    transcript = _transcript()
    transcript.degraded = True
    ir = EditIR(
        project_id="p_test",
        media_facts=_facts(),
        sequence=[_trim("0", "1")],
    )
    assert build_cues(ir, transcript) == []
