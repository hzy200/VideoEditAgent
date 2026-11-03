from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from smartcut.config import Settings
from smartcut.contracts.effects import ColorEffect, FadeEffect
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp, trim_id_for
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.errors import IRValidationError, PlannerResponseError
from smartcut.perception.transcribe import NullTranscriber
from smartcut.planner.client import DeepSeekPlanner
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _facts() -> MediaFacts:
    return MediaFacts(
        src="media/a.mp4",
        duration=_rt("10"),
        fps="30/1",
        video=StreamFacts(
            codec_type="video", width=1280, height=720, avg_frame_rate="30/1"
        ),
        audio=StreamFacts(codec_type="audio", channels=2),
    )


def _draft_json(*, end_seconds: str = "2") -> str:
    return json.dumps(
        {
            "sequence": [
                {
                    "op": "trim",
                    "src": "media/a.mp4",
                    "range": {
                        "start": {"value": 0, "rate": 90_000},
                        "end": {
                            "value": RationalTime.from_seconds(
                                end_seconds, rate=90_000
                            ).value,
                            "rate": 90_000,
                        },
                    },
                    "tag": "main",
                    "confidence": 0.9,
                }
            ],
            "effects": [],
            "audio": {"loudness": None, "remove_filler": False, "bgm": None},
            "export": {
                "preset": "mp4_h264",
                "burn_captions": False,
                "cut_mode": "reencode",
            },
        },
        ensure_ascii=False,
    )


def _settings() -> Settings:
    return Settings(
        workspace_root="./workspace",
        deepseek_api_key="test",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-chat",
    )


def _response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": _draft_json()}}]},
    )


def test_propose_forces_pinned_effects() -> None:
    planner = DeepSeekPlanner(
        _settings(), transport=httpx.MockTransport(lambda _req: _response())
    )
    pinned = [ColorEffect(brightness=0.25)]
    try:
        ir = planner.propose(
            _facts(),
            None,
            "goal",
            project_id="p",
            revision=1,
            pinned_effects=pinned,
        )
    finally:
        planner.close()
    assert len(ir.effects) == 1
    assert ir.effects[0].type == "color"
    assert ir.effects[0].brightness == pytest.approx(0.25)


def test_replan_forces_pinned_effects() -> None:
    planner = DeepSeekPlanner(
        _settings(), transport=httpx.MockTransport(lambda _req: _response())
    )
    base = EditIR(
        project_id="p",
        revision=1,
        media_facts=_facts(),
        sequence=[
            TrimOp(
                src="media/a.mp4",
                range=TimeRange(start=_rt("0"), end=_rt("2")),
            )
        ],
    )
    pinned = [ColorEffect(saturation=0.0)]
    try:
        ir = planner.replan(
            base,
            None,
            "goal",
            {},
            project_id="p",
            revision=2,
            pinned_effects=pinned,
        )
    finally:
        planner.close()
    assert len(ir.effects) == 1
    assert ir.effects[0].type == "color"
    assert ir.effects[0].saturation == pytest.approx(0.0)


def test_pinned_segment_effect_survives_when_range_is_kept() -> None:
    segment_id = trim_id_for(
        "media/a.mp4", TimeRange(start=_rt("0"), end=_rt("2"))
    )
    planner = DeepSeekPlanner(
        _settings(), transport=httpx.MockTransport(lambda _req: _response())
    )
    try:
        ir = planner.propose(
            _facts(),
            None,
            "goal",
            project_id="p",
            revision=1,
            pinned_effects=[
                ColorEffect(
                    brightness=0.2,
                    target="segment",
                    segment_id=segment_id,
                )
            ],
        )
    finally:
        planner.close()
    assert ir.effects[0].target == "segment"
    assert ir.effects[0].segment_id == segment_id
    assert ir.sequence[0].id == segment_id


def test_pinned_segment_effect_fails_when_ai_changes_range() -> None:
    segment_id = trim_id_for(
        "media/a.mp4", TimeRange(start=_rt("0"), end=_rt("2"))
    )
    changed = httpx.Response(
        200,
        json={"choices": [{"message": {"content": _draft_json(end_seconds="3")}}]},
    )
    planner = DeepSeekPlanner(
        _settings(), transport=httpx.MockTransport(lambda _req: changed)
    )
    try:
        with pytest.raises(PlannerResponseError) as exc:
            planner.propose(
                _facts(),
                None,
                "goal",
                project_id="p",
                revision=1,
                pinned_effects=[
                    ColorEffect(
                        target="segment",
                        segment_id=segment_id,
                    )
                ],
            )
    finally:
        planner.close()
    assert "锁定特效" in str(exc.value)


def test_incompatible_pinned_effects_fail_explicitly() -> None:
    planner = DeepSeekPlanner(
        _settings(), transport=httpx.MockTransport(lambda _req: _response())
    )
    try:
        with pytest.raises(PlannerResponseError) as exc:
            planner.propose(
                _facts(),
                None,
                "goal",
                project_id="p",
                revision=1,
                pinned_effects=[FadeEffect(fade_in=_rt("3"), fade_out=_rt("0"))],
            )
    finally:
        planner.close()
    assert "锁定特效" in str(exc.value)


def test_create_run_persists_pinned_effects(
    settings, synthetic_video: Path
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project = services.create_project("pinned")
        services.ingest(project["id"], str(synthetic_video))
        run = services.create_orchestration_run(
            project["id"],
            "goal",
            pinned_effects=[ColorEffect(brightness=0.2)],
        )
        assert run["pinned_effects"]
        assert run["pinned_effects"][0]["type"] == "color"
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_pinned_and_manual_ir_are_mutually_exclusive(
    settings, synthetic_video: Path
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project = services.create_project("pinned-conflict")
        ingested = services.ingest(project["id"], str(synthetic_video))
        facts = MediaFacts.model_validate(ingested["facts"])
        manual = EditIR(
            project_id=project["id"],
            revision=0,
            media_facts=facts,
            sequence=[
                TrimOp(
                    src=ingested["src"],
                    range=TimeRange(start=_rt("0"), end=_rt("1")),
                )
            ],
        )
        with pytest.raises(IRValidationError):
            services.create_orchestration_run(
                project["id"],
                "goal",
                manual_ir=manual,
                pinned_effects=[ColorEffect()],
            )
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
