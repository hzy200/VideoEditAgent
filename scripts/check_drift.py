"""检查代码与机器可读 policy / schema / 打包配置是否漂移。

守卫必须比较**独立事实源**：
- 代码侧白名单（``ENCODER_CODECS``、``TOOLS``、``build_default_agents``、``Phase`` Literal）
- 策略文件（``agents.json`` / ``tools.json`` / ``encoding.json`` / ``workflow.json``）

禁止用“由 policy 派生、再和同一 policy 比较”的恒等式。

用法：
    conda activate video_edit
    python scripts/check_drift.py
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

from smartcut.contracts.encoder import ENCODER_CODECS
from smartcut.contracts.generate import MODELS
from smartcut.contracts.templates import Phase
from smartcut.orchestrator.agents import build_default_agents
from smartcut.policy import load_policy
from smartcut.runner.tools.registry import TOOLS

ROOT = Path(__file__).resolve().parents[1]


def _phase_agents(policy, name: str) -> set[str]:
    return {task.agent for task in policy.workflow.phases[name].tasks}


def check(policy=None) -> list[str]:
    errors: list[str] = []
    policy = policy if policy is not None else load_policy()
    agents = set(policy.agents.agents)

    # 代码白名单 vs 策略文件
    if agents != set(build_default_agents()):
        errors.append("policy agents 与注册子智能体不一致")
    if set(policy.tools.tools) != set(TOOLS):
        errors.append("policy tools 与工具注册表不一致")
    if set(policy.encoding.codecs) != set(ENCODER_CODECS):
        errors.append("policy encoding codecs 与 EncoderProfile 白名单不一致")

    # phase 名称的唯一代码事实源是 contracts.templates.Phase
    phase_names = set(get_args(Phase))
    if set(policy.workflow.phases) != phase_names:
        errors.append(
            "workflow.phases 与 contracts.templates.Phase 不一致："
            f"缺少 {sorted(phase_names - set(policy.workflow.phases))}，"
            f"多出 {sorted(set(policy.workflow.phases) - phase_names)}"
        )

    # 每个已注册 agent 必须至少出现在一个 phase（防止策略删空后静默生效）
    used = {
        task.agent
        for spec in policy.workflow.phases.values()
        for task in spec.tasks
    }
    if agents - used:
        errors.append(f"已注册 agent 未出现在任何 phase: {sorted(agents - used)}")

    # workflow 内部一致性（load_policy 已校验；脚本侧再报一次便于独立定位）
    for name, spec in policy.workflow.phases.items():
        task_ids = {task.id for task in spec.tasks}
        if not set(spec.required) <= _phase_agents(policy, name):
            errors.append(f"{name} 的 required agent 缺少对应 task")
        if set(spec.required) & set(spec.forbidden):
            errors.append(f"{name} 的 required 与 forbidden 冲突")
        if spec.stop_after is not None and spec.stop_after not in task_ids:
            errors.append(f"{name} 的 stop_after 不是本 phase 的 task")
        for task in spec.tasks:
            deps = set(task.depends_on) | set(task.optional_depends_on)
            if deps - task_ids:
                errors.append(f"{name}/{task.id} 依赖不存在的 task: {sorted(deps - task_ids)}")

    # 独立业务不变量：不读取 policy 的期望值，删除关键任务会立即失败。
    if "outline" in policy.workflow.phases:
        outline = policy.workflow.phases["outline"]
        if {"analyze", "chapters"} - _phase_agents(policy, "outline"):
            errors.append("outline 阶段必须包含 analyze 与 chapters")
        if outline.stop_after != "chapters":
            errors.append("outline 阶段必须在 chapters 之后停止")
        forbidden = {"candidates", "plan", "critic", "compile", "render", "verify"}
        if not forbidden <= set(outline.forbidden):
            errors.append("outline 阶段必需 forbidden 集合不完整")

    if "fast_outline" in policy.workflow.phases:
        fast = policy.workflow.phases["fast_outline"]
        required_agents = {"analyze", "frames", "scenes", "contactsheet", "chapters"}
        if not required_agents <= _phase_agents(policy, "fast_outline"):
            errors.append("fast_outline 阶段缺少必需任务")
        if not {"quality", "faces", "transcribe"} <= set(fast.forbidden):
            errors.append("fast_outline 阶段必需 forbidden 集合不完整")

    if "plan" in policy.workflow.phases:
        if {"analyze", "plan"} - _phase_agents(policy, "plan"):
            errors.append("plan 阶段必须包含 analyze 与 plan")
        if {"plan", "critic", "qa", "compile", "render", "verify"} - _phase_agents(
            policy, "plan"
        ):
            errors.append("plan 阶段缺少规划/审片/渲染链")

    if "audio_first" in policy.workflow.phases:
        audio_first = policy.workflow.phases["audio_first"]
        if "transcribe" not in _phase_agents(policy, "audio_first"):
            errors.append("audio_first 阶段必须包含 transcribe")
        frames = next(
            (task for task in audio_first.tasks if task.agent == "frames"),
            None,
        )
        if frames is None:
            errors.append("audio_first 阶段必须包含 frames")
        elif "transcribe" not in (
            set(frames.depends_on) | set(frames.optional_depends_on)
        ):
            errors.append("audio_first 阶段 frames 必须依赖 transcribe")

    if not {
        "outline_approval",
        "visual_notes",
        "image_visual_notes",
        "encoder_profile",
        "edit_review",
    } <= set(MODELS):
        errors.append("contracts.generate.MODELS 缺少 Phase 3 契约")

    spec = (ROOT / "packaging" / "smartcut.spec").read_text(encoding="utf-8")
    if "smartcut/policies" not in spec:
        errors.append("packaging/smartcut.spec 未包含 smartcut/policies")

    return errors


def main() -> int:
    errors = check()
    if errors:
        print("策略漂移检查失败：")
        for item in errors:
            print(f"  - {item}")
        return 1
    print("策略漂移检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
