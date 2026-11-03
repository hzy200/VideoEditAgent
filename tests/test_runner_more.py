from __future__ import annotations

import pytest

from smartcut.cancel import CancellationToken
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.errors import RunCancelledError, SandboxError, VerificationError
from smartcut.runner.paths import Workspace
from smartcut.runner.tools.base import ToolContext
from smartcut.runner.tools.extract_audio import ExtractAudioArgs, build_argv
from smartcut.runner.tools.registry import TOOLS
from smartcut.runner.verify import VerifyArgs, verify_facts


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _facts(duration: str = "2", *, audio: bool = True) -> MediaFacts:
    return MediaFacts(
        src="renders/x.mp4",
        duration=_rt(duration),
        fps="30/1",
        video=StreamFacts(codec_type="video", width=640, height=360),
        audio=StreamFacts(codec_type="audio", channels=2) if audio else None,
    )


def test_verify_accepts_frame_tolerance() -> None:
    expected = VerifyArgs(src="x.mp4", expected_duration=_rt("2"))
    verify_facts(_facts("2.01"), expected)


def test_verify_rejects_duration_mismatch() -> None:
    expected = VerifyArgs(src="x.mp4", expected_duration=_rt("2"))
    with pytest.raises(VerificationError):
        verify_facts(_facts("3"), expected)


def test_verify_rejects_missing_audio() -> None:
    expected = VerifyArgs(src="x.mp4", expected_duration=_rt("2"), expect_audio=True)
    with pytest.raises(VerificationError):
        verify_facts(_facts("2", audio=False), expected)


def test_verify_rejects_dimension_mismatch() -> None:
    expected = VerifyArgs(
        src="x.mp4",
        expected_duration=_rt("2"),
        expected_width=1080,
        expected_height=1920,
    )
    with pytest.raises(VerificationError):
        verify_facts(_facts("2"), expected)


def test_cancellation_token_raises() -> None:
    token = CancellationToken()
    token.raise_if_cancelled()
    token.cancel()
    assert token.cancelled is True
    with pytest.raises(RunCancelledError):
        token.raise_if_cancelled()


def test_workspace_rejects_nul_and_absolute(tmp_path) -> None:
    workspace = Workspace(tmp_path)
    with pytest.raises(SandboxError):
        workspace.resolve("bad\x00path")
    with pytest.raises(SandboxError):
        workspace.resolve(str(tmp_path / "abs.mp4"))


def test_registry_contains_new_mix_tool() -> None:
    assert "mix_audio" in TOOLS
    assert "postprocess" in TOOLS
    assert TOOLS["mix_audio"].timeout_attr == "mix_timeout_seconds"


def test_extract_audio_custom_rate_and_channels(
    settings, workspace, ingested_source: str
) -> None:
    argv = build_argv(
        ExtractAudioArgs(
            src=ingested_source,
            dst="tmp/custom.wav",
            channels=2,
            sample_rate=44_100,
        ),
        ToolContext(settings=settings, workspace=workspace),
    )
    assert argv[argv.index("-ac") + 1] == "2"
    assert argv[argv.index("-ar") + 1] == "44100"
