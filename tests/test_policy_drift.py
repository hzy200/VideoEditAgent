from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from typing import get_args

import pytest

from smartcut.contracts.encoder import ENCODER_CODECS
from smartcut.contracts.generate import MODELS
from smartcut.contracts.templates import Phase
from smartcut.orchestrator.agents import build_default_agents
from smartcut.policy import (
    PHASE_NAMES,
    AgentsPolicy,
    EncodingPolicy,
    Policy,
    ToolsPolicy,
    VisionPolicy,
    WorkflowPolicy,
    load_policy,
    policy_dir,
)
from smartcut.runner.tools.registry import TOOLS

ROOT = Path(__file__).resolve().parents[1]


def _load_drift_module():
    spec = importlib.util.spec_from_file_location(
        "check_drift", ROOT / "scripts" / "check_drift.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_policy(directory: Path, *, validate: bool = True) -> Policy:
    def read(name: str) -> dict:
        return json.loads((directory / name).read_text(encoding="utf-8"))

    policy = Policy(
        agents=AgentsPolicy.model_validate(read("agents.json")),
        tools=ToolsPolicy.model_validate(read("tools.json")),
        encoding=EncodingPolicy.model_validate(read("encoding.json")),
        vision=VisionPolicy.model_validate(read("vision.json")),
        workflow=WorkflowPolicy.model_validate(read("workflow.json")),
    )
    if validate:
        policy.validate_consistency()
    return policy


def _copied_policy(tmp_path: Path) -> Path:
    target = tmp_path / "policy"
    shutil.copytree(policy_dir(), target)
    return target


def _edit_json(path: Path, edit) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def test_policy_matches_registered_agents_and_tools() -> None:
    policy = load_policy()

    assert set(policy.agents.agents) == set(build_default_agents())
    assert set(policy.tools.tools) == set(TOOLS)
    assert set(policy.encoding.codecs) == set(ENCODER_CODECS)


def test_phase_names_come_from_contract_literal() -> None:
    policy = load_policy()

    assert set(policy.workflow.phases) == set(get_args(Phase)) == set(PHASE_NAMES)


def test_workflow_references_known_agents_and_tasks() -> None:
    policy = load_policy()
    known = set(policy.agents.agents)
    for phase, spec in policy.workflow.phases.items():
        assert set(spec.required) <= known, phase
        assert set(spec.forbidden) <= known, phase
        assert {task.agent for task in spec.tasks} <= known, phase


def test_agent_phases_are_derived_from_workflow() -> None:
    policy = load_policy()
    derived = policy.agent_phases()

    assert set(derived) == set(policy.agents.agents)
    assert "outline" in derived["chapters"]
    assert "plan" in derived["candidates"]
    assert "fast_outline" not in derived["quality"]


def test_bad_policy_missing_outline_chapters_fails_drift_check(
    tmp_path: Path,
) -> None:
    policy_dir_path = _copied_policy(tmp_path)

    def remove_chapters(data: dict) -> None:
        phase = data["phases"]["outline"]
        phase["tasks"] = [
            task for task in phase["tasks"] if task["agent"] != "chapters"
        ]
        phase["required"] = [
            item for item in phase["required"] if item != "chapters"
        ]

    _edit_json(policy_dir_path / "workflow.json", remove_chapters)
    policy = _read_policy(policy_dir_path, validate=False)
    errors = _load_drift_module().check(policy)
    assert any("outline" in item and "chapters" in item for item in errors)


def test_bad_policy_missing_plan_agent_fails_drift_check(tmp_path: Path) -> None:
    policy_dir_path = _copied_policy(tmp_path)

    def remove_plan(data: dict) -> None:
        phase = data["phases"]["plan"]
        phase["tasks"] = [
            task for task in phase["tasks"] if task["agent"] != "plan"
        ]
        phase["required"] = [item for item in phase["required"] if item != "plan"]

    _edit_json(policy_dir_path / "workflow.json", remove_plan)
    policy = _read_policy(policy_dir_path, validate=False)
    errors = _load_drift_module().check(policy)
    assert any("plan" in item for item in errors)


def test_unknown_phase_is_rejected_at_load(tmp_path: Path) -> None:
    policy_dir_path = _copied_policy(tmp_path)

    def add_ghost(data: dict) -> None:
        data["phases"]["ghost"] = data["phases"]["plan"]

    _edit_json(policy_dir_path / "workflow.json", add_ghost)
    with pytest.raises(ValueError, match="contracts.templates.Phase"):
        load_policy(policy_dir_path)


def test_unknown_task_dependency_is_rejected_at_load(tmp_path: Path) -> None:
    policy_dir_path = _copied_policy(tmp_path)

    def add_bad_dep(data: dict) -> None:
        data["phases"]["plan"]["tasks"][0]["optional_depends_on"] = ["missing_task"]

    _edit_json(policy_dir_path / "workflow.json", add_bad_dep)
    with pytest.raises(ValueError, match="依赖不存在的 task"):
        load_policy(policy_dir_path)


def test_unused_agent_is_rejected_at_load(tmp_path: Path) -> None:
    policy_dir_path = _copied_policy(tmp_path)

    def add_unused_agent(data: dict) -> None:
        data["agents"]["ghost_agent"] = {"description": "未使用"}

    _edit_json(policy_dir_path / "agents.json", add_unused_agent)
    with pytest.raises(ValueError, match="未出现在任何 workflow phase"):
        load_policy(policy_dir_path)


def test_unknown_view_kind_is_rejected_at_load(tmp_path: Path) -> None:
    policy_dir_path = _copied_policy(tmp_path)

    def bad_kind(data: dict) -> None:
        data["routing"]["types"][0]["primary"] = "heatmap"

    _edit_json(policy_dir_path / "vision.json", bad_kind)
    with pytest.raises(ValueError):
        load_policy(policy_dir_path)


def test_default_route_must_be_contact_sheet(tmp_path: Path) -> None:
    policy_dir_path = _copied_policy(tmp_path)

    def unsafe_default(data: dict) -> None:
        data["routing"]["default"] = "motion_demo"

    _edit_json(policy_dir_path / "vision.json", unsafe_default)
    policy = _read_policy(policy_dir_path, validate=False)
    errors = _load_drift_module().check(policy)
    assert any("default" in item and "contact_sheet" in item for item in errors)


def test_new_contracts_are_exported_in_schemas() -> None:
    assert {
        "candidate_list",
        "chapter_plan",
        "edit_review",
        "encoder_profile",
        "image_visual_notes",
        "outline_approval",
        "screen_text_report",
        "visual_notes",
    } <= set(MODELS)


def test_pyinstaller_spec_includes_policies() -> None:
    spec = ROOT / "packaging" / "smartcut.spec"
    text = spec.read_text(encoding="utf-8")
    assert "smartcut/policies" in text
