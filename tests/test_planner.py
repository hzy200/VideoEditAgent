from __future__ import annotations

import json

import httpx
import pytest

from smartcut.cancel import CancellationToken
from smartcut.config import Settings
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.contracts.transcript import Transcript, TranscriptSegment, Word
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.errors import (
    LLMResponseError,
    PlannerResponseError,
    PlannerUnavailable,
    RunCancelledError,
)
from smartcut.llm.client import DeepSeekClient
from smartcut.orchestrator.models import OrchestrationPlan, OrchestratorTask
from smartcut.orchestrator.planner import TaskPlanner
from smartcut.planner.client import DeepSeekPlanner


def make_facts() -> MediaFacts:
    return MediaFacts(
        src="media/a.mp4",
        duration=RationalTime.from_seconds("10", rate=90_000),
        fps="30/1",
        video=StreamFacts(codec_type="video", width=1280, height=720, avg_frame_rate="30/1"),
        audio=StreamFacts(codec_type="audio", channels=2),
    )


def valid_draft(*, src: str = "media/a.mp4") -> str:
    payload = {
        "sequence": [
            {
                "op": "trim",
                "src": src,
                "range": {
                    "start": {"value": 0, "rate": 90_000},
                    "end": {"value": 180_000, "rate": 90_000},
                },
                "tag": "main",
                "confidence": 0.9,
            }
        ],
        "effects": [],
        "audio": {"loudness": None, "remove_filler": False, "bgm": None},
        "export": {"preset": "mp4_h264", "burn_captions": False, "cut_mode": "reencode"},
    }
    return json.dumps(payload, ensure_ascii=False)


def response_with(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def make_settings(*, api_key: str | None) -> Settings:
    return Settings(
        workspace_root="./workspace",
        deepseek_api_key=api_key,
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-chat",
    )


def test_llm_retries_429_then_succeeds() -> None:
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return response_with(valid_draft())

    settings = make_settings(api_key="test").model_copy(
        update={"deepseek_max_retries": 2, "deepseek_retry_base_seconds": 0.0}
    )
    planner = DeepSeekPlanner(settings, transport=httpx.MockTransport(handler))
    try:
        ir = planner.propose(make_facts(), None, "goal", project_id="p", revision=1)
    finally:
        planner.close()
    assert calls["count"] == 2
    assert ir.sequence


def test_llm_token_budget_blocks_new_requests() -> None:
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )

    settings = make_settings(api_key="test").model_copy(
        update={"deepseek_max_retries": 0, "llm_token_budget": 1}
    )
    client = DeepSeekClient(settings, transport=httpx.MockTransport(handler))
    try:
        assert client.chat_text([{"role": "user", "content": "a"}]) == "ok"
        with pytest.raises(LLMResponseError):
            client.chat_text([{"role": "user", "content": "b"}])
    finally:
        client.close()
    assert calls["count"] == 1


def test_llm_scoped_usage_and_per_run_budget() -> None:
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )

    settings = make_settings(api_key="test").model_copy(
        update={"deepseek_max_retries": 0, "llm_token_budget_per_run": 1}
    )
    client = DeepSeekClient(settings, transport=httpx.MockTransport(handler))
    try:
        client.chat_text(
            [{"role": "user", "content": "a"}],
            usage_scope="run1",
        )
        assert client.usage_for("run1")["total_tokens"] == 5
        with pytest.raises(LLMResponseError):
            client.chat_text(
                [{"role": "user", "content": "b"}],
                usage_scope="run1",
            )
        client.chat_text(
            [{"role": "user", "content": "c"}],
            usage_scope="run2",
        )
        assert client.usage_for("run2")["total_tokens"] == 5
        assert client.usage_for(None)["total_tokens"] == 10
    finally:
        client.close()
    assert calls["count"] == 2


def test_planner_cancel_token_aborts_before_llm() -> None:
    facts = make_facts()

    class Toolkit:
        planner_available = True

        def get_media_facts(self, project_id: str) -> MediaFacts:
            return facts

    planner = TaskPlanner(Toolkit(), make_settings(api_key=None))
    token = CancellationToken()
    token.cancel()
    with pytest.raises(RunCancelledError):
        planner.build(
            "p_test",
            "goal",
            allow_render=False,
            manual_ir=None,
            cancel_token=token,
        )


def test_plan_validation_missing_audio_transcribe_is_value_error() -> None:
    facts = make_facts()

    class Toolkit:
        planner_available = False

        def get_media_facts(self, project_id: str) -> MediaFacts:
            return facts

    planner = TaskPlanner(Toolkit(), make_settings(api_key=None))
    tasks = [
        OrchestratorTask(id="analyze", agent="analyze"),
        OrchestratorTask(id="frames", agent="frames", depends_on=["analyze"]),
        OrchestratorTask(
            id="scenes", agent="scenes", depends_on=["analyze", "frames"]
        ),
        OrchestratorTask(
            id="quality",
            agent="quality",
            depends_on=["analyze", "frames", "scenes"],
        ),
        OrchestratorTask(
            id="chapters",
            agent="chapters",
            depends_on=["analyze", "frames", "scenes", "quality"],
        ),
        OrchestratorTask(
            id="candidates",
            agent="candidates",
            depends_on=["analyze", "frames", "scenes", "quality", "chapters"],
        ),
    ]
    plan = OrchestrationPlan(tasks=tasks)
    with pytest.raises(ValueError, match="transcribe"):
        planner._validate_plan(
            plan,
            facts,
            allow_render=False,
            manual_ir=None,
            phase="plan",
        )

    # 补上 transcribe 后，faces 仍是可选节点，不应抛 KeyError。
    tasks.append(
        OrchestratorTask(id="transcribe", agent="transcribe", depends_on=["analyze"])
    )
    for task in tasks:
        if task.agent in {"chapters", "candidates"}:
            task.depends_on = [*task.depends_on, "transcribe"]
    planner._validate_plan(
        OrchestrationPlan(tasks=tasks),
        facts,
        allow_render=False,
        manual_ir=None,
        phase="plan",
    )


def test_dynamic_agents_follow_phase_conditions() -> None:
    facts = make_facts()

    class Toolkit:
        planner_available = True

        def get_media_facts(self, project_id: str) -> MediaFacts:
            return facts

    planner = TaskPlanner(Toolkit(), make_settings(api_key=None))

    fast = planner.allowed_dynamic_agents(
        "p_test",
        allow_render=True,
        manual_ir=None,
        phase="fast_outline",
    )
    assert "frames" in fast
    assert "quality" not in fast

    no_render = planner.allowed_dynamic_agents(
        "p_test",
        allow_render=False,
        manual_ir=None,
        phase="plan",
    )
    assert "plan" in no_render
    assert "render" not in no_render

    with_render = planner.allowed_dynamic_agents(
        "p_test",
        allow_render=True,
        manual_ir=None,
        phase="plan",
    )
    assert "render" in with_render


def test_llm_single_flight_deduplicates_concurrent_requests() -> None:
    import threading
    import time

    calls = {"count": 0}
    lock = threading.Lock()

    def handler(_request: httpx.Request) -> httpx.Response:
        with lock:
            calls["count"] += 1
        time.sleep(0.1)
        return response_with("same")

    settings = make_settings(api_key="test").model_copy(
        update={"deepseek_max_retries": 0}
    )
    client = DeepSeekClient(settings, transport=httpx.MockTransport(handler))
    results: list[str] = []

    def run() -> None:
        results.append(client.chat_text([{"role": "user", "content": "same"}]))
    threads = [threading.Thread(target=run) for _ in range(3)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        client.close()
    assert calls["count"] == 1
    assert results == ["same", "same", "same"]


def test_allowed_ranges_require_full_containment() -> None:
    facts = make_facts()
    planner = DeepSeekPlanner(make_settings(api_key=None))

    def ir(start: str, end: str) -> EditIR:
        return EditIR(
            project_id="p",
            revision=1,
            media_facts=facts,
            sequence=[
                TrimOp(
                    src=facts.src,
                    range=TimeRange(
                        start=RationalTime.from_seconds(start, rate=90_000),
                        end=RationalTime.from_seconds(end, rate=90_000),
                    ),
                )
            ],
        )

    ranges = [
        RationalTime.from_seconds("0", rate=90_000).as_fraction(),
        RationalTime.from_seconds("1", rate=90_000).as_fraction(),
        RationalTime.from_seconds("2", rate=90_000).as_fraction(),
        RationalTime.from_seconds("3", rate=90_000).as_fraction(),
    ]
    with pytest.raises(ValueError):
        planner._check_draft(ir("0.5", "2.5"), facts, [(ranges[0], ranges[1]), (ranges[2], ranges[3])])
    planner._check_draft(ir("0.5", "0.9"), facts, [(ranges[0], ranges[1]), (ranges[2], ranges[3])])
    # 相邻章节合并后允许跨段。
    planner._check_draft(ir("0.5", "1.5"), facts, [(ranges[0], ranges[1]), (ranges[1], ranges[2])])


def test_planner_repairs_invalid_json_then_succeeds() -> None:
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return response_with('{"sequence": []}')
        return response_with(f"```json\n{valid_draft()}\n```")

    planner = DeepSeekPlanner(make_settings(api_key="test"), transport=httpx.MockTransport(handler))
    try:
        ir = planner.propose(make_facts(), None, "keep the whole clip", project_id="p_test", revision=3)
    finally:
        planner.close()

    assert calls["count"] == 2
    assert ir.project_id == "p_test"
    assert ir.revision == 3
    assert ir.sequence[0].src == "media/a.mp4"


def test_planner_repairs_out_of_bounds() -> None:
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return response_with(valid_draft().replace('"value": 180000', '"value": 999999999'))
        return response_with(valid_draft())

    planner = DeepSeekPlanner(make_settings(api_key="test"), transport=httpx.MockTransport(handler))
    try:
        ir = planner.propose(make_facts(), None, "test", project_id="p_test", revision=1)
    finally:
        planner.close()

    assert calls["count"] == 2
    assert ir.total_duration().to_ffmpeg_seconds() == "2"


def test_planner_gives_up_after_repair_budget() -> None:
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return response_with('{"sequence": []}')

    planner = DeepSeekPlanner(make_settings(api_key="test"), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(PlannerResponseError):
            planner.propose(make_facts(), None, "test", project_id="p_test", revision=1)
    finally:
        planner.close()

    assert calls["count"] == 3


def test_planner_unavailable_without_key() -> None:
    planner = DeepSeekPlanner(make_settings(api_key=None))
    try:
        assert not planner.available
        with pytest.raises(PlannerUnavailable):
            planner.propose(make_facts(), None, "test", project_id="p_test", revision=1)
    finally:
        planner.close()


def test_planner_http_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    planner = DeepSeekPlanner(make_settings(api_key="test"), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(PlannerResponseError):
            planner.propose(make_facts(), None, "test", project_id="p_test", revision=1)
    finally:
        planner.close()


def test_planner_selects_chapters_before_final_plan() -> None:
    facts = MediaFacts(
        src="media/long.mp4",
        duration=RationalTime.from_seconds("1200", rate=90_000),
        fps="30/1",
        video=StreamFacts(codec_type="video", width=1280, height=720, avg_frame_rate="30/1"),
        audio=StreamFacts(codec_type="audio", channels=2),
    )
    chapters = {
        "chapters": [
            {
                "id": "ch_0001",
                "start": {"value": 0, "rate": 90_000},
                "end": {"value": 27_000_000, "rate": 90_000},
            },
            {
                "id": "ch_0002",
                "start": {"value": 27_000_000, "rate": 90_000},
                "end": {"value": 54_000_000, "rate": 90_000},
            },
        ]
    }
    word = Word(
        text="topic",
        start=RationalTime.from_seconds("350", rate=90_000),
        end=RationalTime.from_seconds("380", rate=90_000),
    )
    transcript = Transcript(
        language="en",
        words=[word],
        segments=[
            TranscriptSegment(
                id=0,
                text="topic",
                start=word.start,
                end=word.end,
                words=[word],
            )
        ],
        degraded=False,
    )
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            content = json.dumps(
                {"chapter_ids": ["ch_0002"], "rationale": "relevant chapter"}
            )
        else:
            content = json.dumps(
                {
                    "sequence": [
                        {
                            "op": "trim",
                            "src": facts.src,
                            "range": {
                                "start": {"value": 31_500_000, "rate": 90_000},
                                "end": {"value": 36_000_000, "rate": 90_000},
                            },
                            "tag": "selected",
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
                }
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    planner = DeepSeekPlanner(
        make_settings(api_key="test"),
        transport=httpx.MockTransport(handler),
    )
    try:
        ir = planner.propose(
            facts,
            transcript,
            "找到主题",
            project_id="p_test",
            revision=1,
            chapters=chapters,
        )
    finally:
        planner.close()

    assert calls["count"] == 2
    assert ir.sequence[0].range.start.to_ffmpeg_seconds() == "350"
    assert ir.sequence[0].range.end.to_ffmpeg_seconds() == "400"


def test_llm_client_tracks_token_usage() -> None:
    settings = make_settings(api_key="test")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
            },
        )

    client = DeepSeekClient(settings, transport=httpx.MockTransport(handler))
    try:
        assert client.chat_text([{"role": "user", "content": "a"}]) == "ok"
        assert client.chat_text([{"role": "user", "content": "b"}]) == "ok"
        usage = client.usage_snapshot()
        assert usage["requests"] == 2
        assert usage["prompt_tokens"] == 20
        assert usage["completion_tokens"] == 8
        assert usage["total_tokens"] == 28
    finally:
        client.close()
