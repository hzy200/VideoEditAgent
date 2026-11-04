from __future__ import annotations

from pathlib import Path

import pytest

from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.review import EditReview
from smartcut.contracts.time import RationalTime
from smartcut.planner.evaluation import (
    evaluate_selection,
    ir_intervals,
    load_edl,
)
from smartcut.planner.hybrid import HybridPlanner
from smartcut.planner.rules import RulePlanner, parse_target_seconds

GOLDEN_EDL = Path(__file__).parent / "golden" / "edl"


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _facts(duration: str = "30") -> MediaFacts:
    return MediaFacts(
        src="media/a.mp4",
        duration=_rt(duration),
        fps="30/1",
        video=StreamFacts(
            codec_type="video",
            width=1280,
            height=720,
            avg_frame_rate="30/1",
        ),
    )


def _candidate(
    start: str,
    end: str,
    score: float,
    *,
    tags: list[str] | None = None,
    flags: list[str] | None = None,
) -> dict:
    return {
        "id": f"c_{start}_{end}".replace(".", "_"),
        "source_range": {
            "start": _rt(start).model_dump(),
            "end": _rt(end).model_dump(),
        },
        "score": score,
        "tags": tags or ["topic"],
        "quality_flags": flags or [],
    }


def _planner(tmp_path: Path, **overrides) -> RulePlanner:
    settings = Settings(workspace_root=tmp_path, **overrides)
    return RulePlanner(settings)


def test_trim_rationale_is_optional_and_preserved() -> None:
    base = {
        "op": "trim",
        "src": "media/a.mp4",
        "range": {
            "start": {"value": 0, "rate": 90_000},
            "end": {"value": 90_000, "rate": 90_000},
        },
    }
    legacy = TrimOp.model_validate(base)
    assert legacy.rationale is None
    with_reason = TrimOp.model_validate({**base, "rationale": "命中主题"})
    assert with_reason.rationale == "命中主题"
    assert with_reason.model_dump()["rationale"] == "命中主题"


def test_draft_rationale_is_copied_to_trim_ops() -> None:
    from smartcut.planner.client import _to_trim_ops
    from smartcut.planner.context import EditIRDraft

    draft = EditIRDraft.model_validate(
        {
            "sequence": [
                {
                    "op": "trim",
                    "src": "media/a.mp4",
                    "range": {
                        "start": {"value": 0, "rate": 90_000},
                        "end": {"value": 90_000, "rate": 90_000},
                    },
                    "tag": "主题",
                    "confidence": 0.8,
                    "rationale": "保留核心观点",
                }
            ]
        }
    )
    ops = _to_trim_ops(draft)
    assert ops[0].rationale == "保留核心观点"


def test_planner_rejects_unknown_evidence_label_in_rationale() -> None:
    from smartcut.planner.client import DeepSeekPlanner
    from smartcut.planner.context import EditIRDraft

    planner = DeepSeekPlanner(
        Settings(workspace_root=".", deepseek_api_key=None)
    )
    draft = EditIRDraft.model_validate(
        {
            "sequence": [
                {
                    "op": "trim",
                    "src": "media/a.mp4",
                    "range": {
                        "start": {"value": 0, "rate": 90_000},
                        "end": {"value": 90_000, "rate": 90_000},
                    },
                    "rationale": "依据 T99 高光",
                }
            ]
        }
    )
    try:
        planner._check_draft(
            draft,
            _facts(),
            allowed_labels={"T01"},
        )
    except ValueError as exc:
        assert "T99" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("不存在的证据标签必须被拒绝")
    finally:
        planner.close()


def test_parse_target_seconds_variants() -> None:
    duration = RationalTime.from_seconds("300", rate=90_000).as_fraction()
    assert parse_target_seconds("剪成 60 秒", duration) == (60, False)
    assert parse_target_seconds("保留 2 分钟", duration) == (120, False)
    assert parse_target_seconds("1 分 30 秒", duration) == (90, False)
    target, prefer_start = parse_target_seconds("保留前 10 秒", duration)
    assert (target, prefer_start) == (10, True)
    assert parse_target_seconds("精彩片段", duration) == (duration, False)
    # 超过素材时长时夹紧。
    assert parse_target_seconds("10 分钟", duration)[0] == duration


def test_rule_planner_selects_by_score_then_orders_by_source(tmp_path: Path) -> None:
    planner = _planner(tmp_path)
    candidates = {
        "candidates": [
            _candidate("10", "15", 0.9, tags=["high"]),
            _candidate("0", "5", 0.8, tags=["opening"]),
            _candidate("20", "25", 0.2, tags=["low"]),
        ]
    }
    ir = planner.propose(
        _facts(),
        None,
        "保留 10 秒",
        project_id="p",
        revision=1,
        candidates=candidates,
    )
    assert [op.tag for op in ir.sequence] == ["opening", "high"]
    assert [
        (op.range.start.to_float(), op.range.end.to_float())
        for op in ir.sequence
    ] == [(0.0, 5.0), (10.0, 15.0)]
    assert all(op.rationale for op in ir.sequence)
    assert all(op.src == "media/a.mp4" for op in ir.sequence)


def test_rule_planner_excludes_quality_flags_and_marks(tmp_path: Path) -> None:
    planner = _planner(tmp_path)
    candidates = {
        "candidates": [
            _candidate("0", "5", 0.9, flags=["blurry"]),
            _candidate("5", "10", 0.8),
            _candidate("10", "15", 0.95),
        ]
    }
    perception = {
        "quality": {
            "marks": [
                {"t": _rt("12").model_dump(), "kind": "black", "score": 0.9}
            ]
        }
    }
    ir = planner.propose(
        _facts(),
        None,
        "保留 15 秒",
        project_id="p",
        revision=1,
        candidates=candidates,
        perception=perception,
    )
    assert [(op.range.start.to_float(), op.range.end.to_float()) for op in ir.sequence] == [
        (5.0, 10.0)
    ]


def test_rule_planner_trims_overlapping_candidates(tmp_path: Path) -> None:
    planner = _planner(tmp_path)
    candidates = {
        "candidates": [
            _candidate("0", "6", 0.9),
            _candidate("4", "8", 0.8),
        ]
    }
    ir = planner.propose(
        _facts(),
        None,
        "保留 10 秒",
        project_id="p",
        revision=1,
        candidates=candidates,
    )
    intervals = [
        (op.range.start.as_fraction(), op.range.end.as_fraction())
        for op in ir.sequence
    ]
    assert intervals == [
        (RationalTime.from_seconds("0", rate=90_000).as_fraction(), RationalTime.from_seconds("6", rate=90_000).as_fraction()),
        (RationalTime.from_seconds("6", rate=90_000).as_fraction(), RationalTime.from_seconds("8", rate=90_000).as_fraction()),
    ]


def test_rule_planner_splits_long_segments(tmp_path: Path) -> None:
    planner = _planner(tmp_path, critic_max_segment_seconds=90)
    candidates = {"candidates": [_candidate("0", "200", 0.9)]}
    ir = planner.propose(
        _facts("300"),
        None,
        "保留全部",
        project_id="p",
        revision=1,
        candidates=candidates,
    )
    durations = [op.range.duration().to_float() for op in ir.sequence]
    assert len(durations) == 3
    assert max(durations) <= 90
    assert sum(durations) == pytest.approx(200)


def test_rule_planner_respects_approved_chapters(tmp_path: Path) -> None:
    planner = _planner(tmp_path)
    chapters = {
        "chapters": [
            {
                "id": "ch_1",
                "start": _rt("0").model_dump(),
                "end": _rt("10").model_dump(),
                "title": "开头",
            },
            {
                "id": "ch_2",
                "start": _rt("10").model_dump(),
                "end": _rt("20").model_dump(),
                "title": "重点",
            },
        ]
    }
    ir = planner.propose(
        _facts(),
        None,
        "保留 20 秒",
        project_id="p",
        revision=1,
        chapters=chapters,
        chapter_ids=["ch_2"],
    )
    assert [(op.range.start.to_float(), op.range.end.to_float()) for op in ir.sequence] == [
        (10.0, 20.0)
    ]
    assert ir.sequence[0].tag == "chapter"
    assert "重点" in (ir.sequence[0].rationale or "")


def test_hybrid_planner_falls_back_without_llm(tmp_path: Path) -> None:
    settings = Settings(workspace_root=tmp_path, deepseek_api_key=None)
    planner = HybridPlanner(settings)
    try:
        assert planner.available is True
        assert planner.llm_available is False
        ir = planner.propose(
            _facts(),
            None,
            "保留前 1 秒",
            project_id="p",
            revision=1,
        )
        assert ir.sequence
        assert ir.sequence[0].range.start.to_float() == 0.0
        assert ir.sequence[0].range.end.to_float() == 1.0
        unchanged = planner.replan(
            ir,
            None,
            "保留前 1 秒",
            {},
            project_id="p",
            revision=2,
        )
        assert unchanged.sequence == ir.sequence
        assert unchanged.revision == 2
        assert planner.review(ir, None, "goal").score >= 0.2
    finally:
        planner.close()


class _FakeLLM:
    available = True

    def __init__(self, ir: EditIR) -> None:
        self.ir = ir
        self.proposed = 0

    def propose(self, facts, transcript, goal, **kwargs) -> EditIR:
        self.proposed += 1
        return self.ir

    def replan(self, ir, transcript, goal, review, **kwargs) -> EditIR:
        return ir

    def review(self, ir, transcript, goal, **kwargs) -> EditReview:
        return EditReview(score=0.95, summary="llm")

    def usage_snapshot(self) -> dict[str, int]:
        return {}

    def usage_for(self, scope: str | None) -> dict[str, int]:
        return {}

    def close(self) -> None:
        return None


def test_hybrid_planner_delegates_when_llm_available(tmp_path: Path) -> None:
    facts = _facts()
    llm_ir = EditIR(
        project_id="p",
        revision=1,
        media_facts=facts,
        sequence=[
            TrimOp(
                src=facts.src,
                range=TimeRange(start=_rt("2"), end=_rt("4")),
                tag="llm",
            )
        ],
    )
    fake = _FakeLLM(llm_ir)
    planner = HybridPlanner(
        Settings(workspace_root=tmp_path, deepseek_api_key="test"),
        llm=fake,  # type: ignore[arg-type]
    )
    ir = planner.propose(facts, None, "goal", project_id="p", revision=1)
    assert fake.proposed == 1
    assert ir.sequence[0].tag == "llm"
    assert planner.review(ir, None, "goal").score == 0.95


@pytest.mark.eval
@pytest.mark.parametrize(
    "edl_name",
    ["synthetic_keep_first_second.json", "synthetic_keep_all.json"],
)
def test_rule_selection_meets_edl_f1(
    edl_name: str,
    settings: Settings,
    executor,
    ingested_source: str,
) -> None:
    edl = load_edl(GOLDEN_EDL / edl_name)
    facts = executor.probe(ingested_source)
    planner = RulePlanner(settings)
    ir = planner.propose(
        facts,
        None,
        str(edl["goal"]),
        project_id="p_eval",
        revision=1,
    )
    metrics = evaluate_selection(
        ir_intervals(ir),
        edl["kept_intervals"],
        source_duration=facts.duration.to_float(),
    )
    assert metrics["f1"] >= 0.95, metrics
    assert metrics["duplicate_ratio"] == 0.0
