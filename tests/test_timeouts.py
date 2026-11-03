from __future__ import annotations

from smartcut.config import Settings
from smartcut.runner.timeouts import compute_tool_timeout, tool_default_timeout


def _settings(**overrides) -> Settings:
    values = {
        "tool_timeout_seconds": 120,
        "mix_timeout_seconds": 600,
        "render_timeout_seconds": 900,
    }
    values.update(overrides)
    return Settings(**values)


def test_unscaled_tool_uses_default_timeout() -> None:
    settings = _settings()
    assert (
        compute_tool_timeout(
            "verify",
            duration_seconds=7200,
            width=3840,
            height=2160,
            settings=settings,
        )
        == 120
    )


def test_duration_and_resolution_scale_render_timeout() -> None:
    settings = _settings()
    short = compute_tool_timeout(
        "cut",
        duration_seconds=2,
        width=1920,
        height=1080,
        settings=settings,
    )
    long = compute_tool_timeout(
        "cut",
        duration_seconds=7200,
        width=1920,
        height=1080,
        settings=settings,
    )
    assert long > short
    assert long == settings.render_timeout_seconds


def test_higher_resolution_gets_longer_timeout() -> None:
    settings = _settings()
    hd = compute_tool_timeout(
        "postprocess",
        duration_seconds=60,
        width=1920,
        height=1080,
        settings=settings,
    )
    uhd = compute_tool_timeout(
        "postprocess",
        duration_seconds=60,
        width=3840,
        height=2160,
        settings=settings,
    )
    assert uhd > hd


def test_compose_scales_from_tool_floor_to_render_cap() -> None:
    settings = _settings()
    short = compute_tool_timeout(
        "compose",
        duration_seconds=2,
        width=1920,
        height=1080,
        settings=settings,
    )
    long = compute_tool_timeout(
        "compose",
        duration_seconds=7200,
        width=1920,
        height=1080,
        settings=settings,
    )
    assert short < settings.render_timeout_seconds
    assert long == settings.render_timeout_seconds


def test_mix_uses_its_own_floor() -> None:
    settings = _settings(render_timeout_seconds=300)
    assert tool_default_timeout("mix_audio", settings) == 600
    assert (
        compute_tool_timeout(
            "mix_audio",
            duration_seconds=1,
            width=320,
            height=240,
            settings=settings,
        )
        >= 600
    )
