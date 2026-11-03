from __future__ import annotations

from fractions import Fraction

import pytest
from pydantic import TypeAdapter, ValidationError

from smartcut.contracts.captions import CaptionCue
from smartcut.contracts.effects import Effect
from smartcut.contracts.edit_ir import AudioSpec, EditIR, ExportSpec, TimeRange, TrimOp
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime, fps_from_string, sum_rational_times
from smartcut.contracts.transcript import Word


def make_facts(src: str = "media/a.mp4", duration: str = "10", fps: str = "30/1") -> MediaFacts:
    return MediaFacts(
        src=src,
        duration=RationalTime.from_seconds(duration, rate=90_000),
        fps=fps,
        video=StreamFacts(
            codec_type="video",
            width=1920,
            height=1080,
            avg_frame_rate=fps,
            r_frame_rate=fps,
        ),
        audio=StreamFacts(codec_type="audio", channels=2, sample_rate=48_000),
    )


def make_ir(*, effects: list[Effect] | None = None, export: ExportSpec | None = None) -> EditIR:
    facts = make_facts()
    return EditIR(
        project_id="p_test",
        revision=1,
        media_facts=facts,
        sequence=[
            TrimOp(
                src=facts.src,
                range=TimeRange(
                    start=RationalTime.from_seconds("0.5", rate=90_000),
                    end=RationalTime.from_seconds("1.5", rate=90_000),
                ),
            )
        ],
        effects=effects or [],
        audio=AudioSpec(),
        export=export or ExportSpec(),
    )


def test_rational_time_basic_math() -> None:
    a = RationalTime(value=45_000, rate=90_000)
    b = RationalTime.from_seconds("0.5", rate=90_000)
    assert a == b
    assert a.to_ffmpeg_seconds() == "0.5"
    assert (a + b) == RationalTime.from_seconds("1", rate=90_000)


def test_rational_time_frame_exact_2997() -> None:
    fps = fps_from_string("30000/1001")
    one_frame = RationalTime.from_frames(1, fps)
    assert one_frame.as_fraction() == Fraction(1001, 30000)
    snapped = RationalTime.from_seconds("0.0333", rate=90_000).snap_to_frame(fps)
    assert snapped.as_fraction() == Fraction(1001, 30000)


def test_sum_rational_times_and_rate_conversion() -> None:
    values = [
        RationalTime.from_seconds("0.5", rate=90_000),
        RationalTime.from_seconds("1.25", rate=90_000),
    ]
    total = sum_rational_times(values).round_to_rate(90_000)
    assert total.value == 157_500
    assert total.rate == 90_000


def test_fps_parser_rejects_invalid() -> None:
    assert fps_from_string("30") == Fraction(30, 1)
    with pytest.raises(ValueError):
        fps_from_string("0/0")


def test_time_range_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        TimeRange(
            start=RationalTime.from_seconds("2", rate=90_000),
            end=RationalTime.from_seconds("1", rate=90_000),
        )


def test_edit_ir_total_duration() -> None:
    assert make_ir().total_duration().to_ffmpeg_seconds() == "1"


def test_effect_must_not_change_duration() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Effect).validate_python({"type": "speed"})


def test_edit_ir_rejects_cross_source() -> None:
    facts = make_facts()
    with pytest.raises(ValidationError):
        EditIR(
            project_id="p_test",
            media_facts=facts,
            sequence=[
                TrimOp(
                    src="media/other.mp4",
                    range=TimeRange(
                        start=RationalTime.from_seconds("0", rate=90_000),
                        end=RationalTime.from_seconds("1", rate=90_000),
                    ),
                )
            ],
        )


def test_edit_ir_rejects_unsafe_project_id() -> None:
    facts = make_facts()
    with pytest.raises(ValidationError):
        EditIR(
            project_id="../evil",
            media_facts=facts,
            sequence=[
                TrimOp(
                    src=facts.src,
                    range=TimeRange(
                        start=RationalTime.from_seconds("0", rate=90_000),
                        end=RationalTime.from_seconds("1", rate=90_000),
                    ),
                )
            ],
        )


def test_audio_adjustment_defaults_and_ranges() -> None:
    ir = make_ir()
    assert ir.audio.volume_db == 0.0
    assert ir.audio.pitch_semitones == 0.0
    assert ir.subtitles is None
    with pytest.raises(ValidationError):
        AudioSpec(volume_db=13)
    with pytest.raises(ValidationError):
        AudioSpec(pitch_semitones=-13)

    manual = make_ir()
    manual.subtitles = [
        CaptionCue(
            start=RationalTime.from_seconds("0", rate=90_000),
            end=RationalTime.from_seconds("1", rate=90_000),
            text="hi",
        )
    ]
    dumped = manual.dump_ir()
    assert dumped["subtitles"][0]["text"] == "hi"
    assert EditIR.model_validate(dumped).subtitles[0].text == "hi"


def test_word_span_ordered() -> None:
    with pytest.raises(ValidationError):
        Word(
            text="x",
            start=RationalTime.from_seconds("1", rate=90_000),
            end=RationalTime.from_seconds("0.5", rate=90_000),
        )


def test_rational_time_accepts_numpy_scalar() -> None:
    np = pytest.importorskip("numpy")
    value = np.float64(0.5)
    assert RationalTime.from_seconds(value, rate=90_000).value == 45_000
