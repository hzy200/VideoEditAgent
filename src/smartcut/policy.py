"""机器可读策略：约束 agent、工具、编码器与视觉输入。

策略文件随包分发（``smartcut/policies``），可用 ``SMARTCUT_POLICY_DIR`` 覆盖。
代码与策略的漂移由 ``scripts/check_drift.py`` 与 ``tests/test_policy_drift.py`` 检查。
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.ids import SafeId
from smartcut.contracts.templates import Phase

# phase 名称的唯一代码事实源：由契约 Literal 派生。
PHASE_NAMES: frozenset[str] = frozenset(get_args(Phase))


class AgentPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str


class AgentsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    agents: dict[str, AgentPolicy]


class ToolsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    tools: list[str]


class EncoderPolicyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: str
    quality: int = Field(ge=0, le=51)


class EncodingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    codecs: dict[str, EncoderPolicyEntry]
    priority_default: list[str]
    probe_args: list[str]
    error_keywords: list[str]


class VisionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    prompt_version: str
    max_images: int = Field(ge=1)
    image_max_edge: int = Field(ge=64)
    image_max_bytes: int = Field(ge=1024)
    total_max_bytes: int = Field(ge=1024)
    max_keyframes_per_chapter: int = Field(ge=0)
    text_first: bool = True
    text_min_chars: int = Field(default=40, ge=0)
    chapter_tiles: bool = True
    second_pass_cautions: int = Field(default=2, ge=0)
    mime_types: list[str]


TaskCondition = Literal["always", "video", "audio", "plan", "render"]


class TaskSpec(BaseModel):
    """规则任务图节点；依赖只引用同一 phase 内的 task id。"""

    model_config = ConfigDict(extra="forbid")

    id: SafeId
    agent: SafeId
    when: TaskCondition = "always"
    depends_on: list[str] = Field(default_factory=list)
    optional_depends_on: list[str] = Field(default_factory=list)
    allow_failed_deps: bool = False
    description: str = ""


class PhasePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stop_after: str | None = None
    required: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    rationale: str = ""
    tasks: list[TaskSpec] = Field(default_factory=list)


class WorkflowPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    phases: dict[str, PhasePolicy]


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agents: AgentsPolicy
    tools: ToolsPolicy
    encoding: EncodingPolicy
    vision: VisionPolicy
    workflow: WorkflowPolicy

    def agent_phases(self) -> dict[str, frozenset[str]]:
        """由 workflow 推导 agent → phase 归属，避免第二份事实源。"""
        return {
            name: frozenset(
                phase
                for phase, spec in self.workflow.phases.items()
                if any(task.agent == name for task in spec.tasks)
            )
            for name in self.agents.agents
        }

    def validate_consistency(self) -> None:
        names = set(self.agents.agents)
        phases = set(self.workflow.phases)
        if phases != PHASE_NAMES:
            raise ValueError(
                "workflow.phases 必须与 contracts.templates.Phase 完全一致："
                f"缺少 {sorted(PHASE_NAMES - phases)}，多出 {sorted(phases - PHASE_NAMES)}"
            )

        used_agents: set[str] = set()
        for phase, spec in self.workflow.phases.items():
            missing = (set(spec.required) | set(spec.forbidden)) - names
            if missing:
                raise ValueError(
                    f"workflow.phases[{phase}] 引用了未知 agent: {sorted(missing)}"
                )
            if set(spec.required) & set(spec.forbidden):
                raise ValueError(f"workflow.phases[{phase}] required 与 forbidden 冲突")

            task_ids = [task.id for task in spec.tasks]
            if len(task_ids) != len(set(task_ids)):
                raise ValueError(f"workflow.phases[{phase}] 存在重复 task id")
            phase_agents = {task.agent for task in spec.tasks}
            unknown_agents = phase_agents - names
            if unknown_agents:
                raise ValueError(
                    f"workflow.phases[{phase}] tasks 引用未知 agent: "
                    f"{sorted(unknown_agents)}"
                )
            missing_required = set(spec.required) - phase_agents
            if missing_required:
                raise ValueError(
                    f"workflow.phases[{phase}] required agent 没有对应 task: "
                    f"{sorted(missing_required)}"
                )
            if spec.stop_after is not None and spec.stop_after not in set(task_ids):
                raise ValueError(
                    f"workflow.phases[{phase}] stop_after 不是本 phase 的 task: "
                    f"{spec.stop_after!r}"
                )
            for task in spec.tasks:
                unknown_deps = (
                    set(task.depends_on) | set(task.optional_depends_on)
                ) - set(task_ids)
                if unknown_deps:
                    raise ValueError(
                        f"workflow.phases[{phase}] task {task.id} 依赖不存在的 task: "
                        f"{sorted(unknown_deps)}"
                    )
            used_agents |= phase_agents

        unused_agents = names - used_agents
        if unused_agents:
            raise ValueError(
                f"已注册 agent 未出现在任何 workflow phase: {sorted(unused_agents)}"
            )
        missing_descriptions = {
            name for name, item in self.agents.agents.items() if not item.description
        }
        if missing_descriptions:
            raise ValueError(
                f"agent 缺少 description: {sorted(missing_descriptions)}"
            )
        if set(self.encoding.priority_default) - set(self.encoding.codecs):
            raise ValueError("encoding.priority_default 包含未定义 codec")
        if not set(self.encoding.codecs) >= {"libx264"}:
            raise ValueError("encoding.codecs 必须包含 libx264")


def _package_dir() -> Path:
    from importlib.resources import files

    return Path(str(files("smartcut.policies")))


def policy_dir(override: str | Path | None = None) -> Path:
    value = override or os.getenv("SMARTCUT_POLICY_DIR")
    return Path(value).expanduser().resolve() if value else _package_dir()


@lru_cache(maxsize=4)
def _load_from(directory: str) -> Policy:
    root = Path(directory)

    def read(name: str) -> dict:
        return json.loads((root / name).read_text(encoding="utf-8"))

    policy = Policy(
        agents=AgentsPolicy.model_validate(read("agents.json")),
        tools=ToolsPolicy.model_validate(read("tools.json")),
        encoding=EncodingPolicy.model_validate(read("encoding.json")),
        vision=VisionPolicy.model_validate(read("vision.json")),
        workflow=WorkflowPolicy.model_validate(read("workflow.json")),
    )
    policy.validate_consistency()
    return policy


def load_policy(directory: str | Path | None = None) -> Policy:
    return _load_from(str(policy_dir(directory)))
