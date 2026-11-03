from __future__ import annotations

import pytest
from pydantic import ValidationError

from smartcut.contracts.audio import AudioAssetRef, AudioTrackSpec
from smartcut.contracts.captions import CaptionCue
from smartcut.contracts.edit_ir import AudioSpec, EditIR, ExportSpec, TimeRange, TrimOp
from smartcut.contracts.encoder import ENCODER_CODECS, EncoderProfile
from smartcut.contracts.generate import build_schemas
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime


def _facts(duration: str = "10") -> MediaFacts:
    return MediaFacts(
        src="media/a.mp4",
        duration=RationalTime.from_seconds(duration, rate=90_000),
        fps="30/1",
        video=StreamFacts(codec_type="video", width=1920, height=1080),
        audio=StreamFacts(codec_type="audio", channels=2, sample_rate=48_000),
    )


def _trim(start: str, end: str) -> TrimOp:
    return TrimOp(
        src="media/a.mp4",
        range=TimeRange(
            start=RationalTime.from_seconds(start, rate=90_000),
            end=RationalTime.from_seconds(end, rate=90_000),
        ),
    )


def _track(track_id: str = "t_01", **overrides) -> AudioTrackSpec:
    payload = {"id": track_id, "asset_id": "a_bgm"}
    payload.update(overrides)
    return AudioTrackSpec(**payload)


def test_audio_spec_rejects_duplicate_track_ids() -> None:
    with pytest.raises(ValidationError):
        AudioSpec(tracks=[_track("same"), _track("same", asset_id="other")])


def test_audio_track_validates_ranges_and_loop() -> None:
    with pytest.raises(ValidationError):
        _track(source_start=RationalTime.from_seconds("2", rate=90_000),
               source_end=RationalTime.from_seconds("1", rate=90_000))
    with pytest.raises(ValidationError):
        _track(timeline_start=RationalTime.from_seconds("-1", rate=90_000))
    with pytest.raises(ValidationError):
        _track(fade_in=RationalTime.from_seconds("-1", rate=90_000))
    track = _track(loop=True)
    assert track.loop is True


@pytest.mark.parametrize("gain", [-61, 13])
def test_audio_track_gain_bounds(gain: float) -> None:
    with pytest.raises(ValidationError):
        _track(gain_db=gain)


def test_caption_cue_rejects_empty_and_reversed() -> None:
    with pytest.raises(ValidationError):
        CaptionCue(
            start=RationalTime.from_seconds("0", rate=90_000),
            end=RationalTime.from_seconds("1", rate=90_000),
            text="   ",
        )
    with pytest.raises(ValidationError):
        CaptionCue(
            start=RationalTime.from_seconds("2", rate=90_000),
            end=RationalTime.from_seconds("1", rate=90_000),
            text="x",
        )


def test_edit_ir_distinguishes_auto_and_empty_subtitles() -> None:
    auto = EditIR(project_id="p_test", media_facts=_facts(), sequence=[_trim("0", "1")])
    assert auto.subtitles is None
    cleared = auto.model_copy(update={"subtitles": []})
    assert cleared.subtitles == []
    assert cleared.dump_ir()["subtitles"] == []


def test_edit_ir_total_duration_multiple_trims() -> None:
    ir = EditIR(
        project_id="p_test",
        media_facts=_facts(),
        sequence=[_trim("0", "1.5"), _trim("3", "5")],
    )
    assert ir.total_duration().to_ffmpeg_seconds() == "3.5"


def test_encoder_profile_whitelist_and_quality_bounds() -> None:
    for codec in ENCODER_CODECS:
        profile = EncoderProfile(codec=codec, quality=20)
        assert profile.codec == codec
    with pytest.raises(ValidationError):
        EncoderProfile(codec="h264_unknown")
    with pytest.raises(ValidationError):
        EncoderProfile(codec="libx264", quality=60)


def test_audio_asset_ref_defaults() -> None:
    ref = AudioAssetRef(
        id="main",
        src="media/a.mp4",
        duration=RationalTime.from_seconds("1", rate=90_000),
    )
    assert ref.channels == 2
    assert ref.sample_rate == 48_000
    assert ref.has_audio is True


def test_empty_sequence_and_invalid_cut_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        EditIR(project_id="p_test", media_facts=_facts(), sequence=[])
    with pytest.raises(ValidationError):
        ExportSpec(cut_mode="invalid")


def test_schema_export_includes_audio_contracts() -> None:
    schemas = build_schemas()
    assert "audio_asset" in schemas
    assert "audio_track" in schemas
    assert "edit_ir" in schemas
    assert "loop" in schemas["audio_track"]["properties"]
