from __future__ import annotations

import json
from typing import Any

import httpx

from smartcut.config import Settings
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.llm.client import DeepSeekClient
from smartcut.orchestrator.planner import TaskPlanner


def make_facts() -> MediaFacts:
    return MediaFacts(
        src="media/a.mp4",
        duration=RationalTime.from_seconds("10", rate=90_000),
        fps="30/1",
        video=StreamFacts(codec_type="video", width=1280, height=720, avg_frame_rate="30/1"),
        audio=StreamFacts(codec_type="audio", channels=2),
    )


class FakeToolkit:
    def __init__(self, facts: MediaFacts, *, planner_available: bool = True) -> None:
        self._facts = facts
        self.planner_available = planner_available

    def get_media_facts(self, project_id: str) -> MediaFacts:
        return self._facts


def make_settings(*, api_key: str | None = "test", use_llm: bool = True) -> Settings:
    return Settings(
        workspace_root="./workspace",
        deepseek_api_key=api_key,
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-chat",
        orchestrator_use_llm=use_llm,
    )


def make_client(settings: Settings, content: str, counter: dict[str, int]) -> DeepSeekClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        counter["calls"] = counter.get("calls", 0) + 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    return DeepSeekClient(settings, transport=httpx.MockTransport(handler))


VALID_PLAN = {
    "rationale": "LLM plan",
    "tasks": [
        {"id": "analyze", "agent": "analyze"},
        {"id": "frames", "agent": "frames", "depends_on": ["analyze"]},
        {"id": "scenes", "agent": "scenes", "depends_on": ["frames"]},
        {"id": "quality", "agent": "quality", "depends_on": ["frames", "scenes"]},
        {"id": "faces", "agent": "faces", "depends_on": ["frames", "scenes"]},
        {"id": "contactsheet", "agent": "contactsheet", "depends_on": ["frames"]},
        {"id": "transcribe", "agent": "transcribe"},
        {
            "id": "chapters",
            "agent": "chapters",
            "depends_on": ["frames", "scenes", "quality", "faces", "transcribe"],
        },
        {
            "id": "candidates",
            "agent": "candidates",
            "depends_on": ["frames", "scenes", "quality", "faces", "chapters", "transcribe"],
        },
        {
            "id": "plan",
            "agent": "plan",
            "depends_on": [
                "analyze",
                "frames",
                "scenes",
                "quality",
                "faces",
                "contactsheet",
                "chapters",
                "candidates",
                "transcribe",
            ],
        },
        {"id": "audio", "agent": "audio", "depends_on": ["plan"]},
        {"id": "critic", "agent": "critic", "depends_on": ["audio"]},
        {"id": "qa", "agent": "qa", "depends_on": ["critic"]},
        {"id": "compile", "agent": "compile", "depends_on": ["qa"]},
        {"id": "render", "agent": "render", "depends_on": ["compile"]},
        {"id": "verify", "agent": "verify", "depends_on": ["render"]},
    ],
}

INVALID_PLAN = {
    "rationale": "bad",
    "tasks": [
        {"id": "analyze", "agent": "analyze"},
        {"id": "transcribe", "agent": "transcribe"},
        {"id": "render", "agent": "render"},
    ],
}


def test_default_settings_use_rule_task_graph() -> None:
    assert Settings().orchestrator_use_llm is False


def test_rule_plan_contains_parallel_analysis_and_render_chain() -> None:
    facts = make_facts()
    toolkit = FakeToolkit(facts, planner_available=True)
    planner = TaskPlanner(toolkit, make_settings(use_llm=False))
    plan = planner.build(
        "p_test",
        "剪一段",
        allow_render=True,
        manual_ir=None,
    )
    by_agent = {task.agent: task for task in plan.tasks}
    assert set(by_agent) == {
        "analyze",
        "frames",
        "scenes",
        "quality",
        "faces",
        "contactsheet",
        "chapters",
        "candidates",
        "transcribe",
        "plan",
        "audio",
        "critic",
        "qa",
        "replan",
        "compile",
        "render",
        "verify",
    }
    assert by_agent["transcribe"].depends_on == []
    assert by_agent["frames"].depends_on == ["analyze"]
    assert by_agent["scenes"].depends_on == ["frames"]
    assert by_agent["quality"].depends_on == ["frames", "scenes"]
    assert by_agent["faces"].depends_on == ["frames", "scenes"]
    assert set(by_agent["plan"].depends_on) == {
        "analyze",
        "frames",
        "scenes",
        "quality",
        "faces",
        "contactsheet",
        "chapters",
        "candidates",
        "transcribe",
    }
    assert by_agent["candidates"].depends_on == [
        "frames",
        "scenes",
        "quality",
        "faces",
        "chapters",
        "transcribe",
    ]
    assert by_agent["audio"].depends_on == ["plan"]
    assert by_agent["critic"].depends_on == ["audio"]
    assert by_agent["qa"].depends_on == ["critic"]
    assert by_agent["replan"].depends_on == ["qa"]
    assert by_agent["compile"].depends_on == ["replan"]
    assert by_agent["verify"].depends_on == ["render"]


def test_rule_plan_without_planner_stops_before_edit() -> None:
    facts = make_facts()
    toolkit = FakeToolkit(facts, planner_available=False)
    planner = TaskPlanner(toolkit, make_settings(api_key=None, use_llm=False))
    plan = planner.build("p_test", "分析", allow_render=True, manual_ir=None)
    agents = [task.agent for task in plan.tasks]
    assert {
        "analyze",
        "frames",
        "scenes",
        "quality",
        "faces",
        "contactsheet",
        "chapters",
        "candidates",
        "transcribe",
    } <= set(agents)
    assert "plan" not in agents
    assert "critic" not in agents
    assert "render" not in agents
    assert "未配置 DeepSeek" in plan.rationale


def test_llm_plan_is_used_when_valid() -> None:
    settings = make_settings()
    facts = make_facts()
    toolkit = FakeToolkit(facts, planner_available=True)
    counter: dict[str, int] = {}
    llm = make_client(settings, json.dumps(VALID_PLAN), counter)
    try:
        planner = TaskPlanner(toolkit, settings, llm)
        plan = planner.build("p_test", "剪一段", allow_render=True, manual_ir=None)
    finally:
        llm.close()
    assert plan.rationale == "LLM plan"
    assert counter["calls"] == 1


def test_invalid_llm_plan_falls_back_to_rules() -> None:
    settings = make_settings()
    facts = make_facts()
    toolkit = FakeToolkit(facts, planner_available=True)
    counter: dict[str, int] = {}
    llm = make_client(settings, json.dumps(INVALID_PLAN), counter)
    try:
        planner = TaskPlanner(toolkit, settings, llm)
        plan = planner.build("p_test", "剪一段", allow_render=True, manual_ir=None)
    finally:
        llm.close()
    assert plan.rationale.startswith("规则规划")
    assert counter["calls"] == 2  # 1 次原始 + 1 次修复


def test_llm_plan_cannot_render_when_disabled() -> None:
    settings = make_settings()
    facts = make_facts()
    toolkit = FakeToolkit(facts, planner_available=True)
    counter: dict[str, int] = {}
    llm = make_client(settings, json.dumps(VALID_PLAN), counter)
    try:
        planner = TaskPlanner(toolkit, settings, llm)
        plan = planner.build("p_test", "只分析", allow_render=False, manual_ir=None)
    finally:
        llm.close()
    assert "render" not in {task.agent for task in plan.tasks}
    assert plan.rationale.startswith("规则规划")
