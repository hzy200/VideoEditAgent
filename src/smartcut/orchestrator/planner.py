"""任务规划：LLM 动态构建任务图；失败或不可用时回退到规则图。"""

from __future__ import annotations

import json
from typing import Any

from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.media import MediaFacts
from smartcut.errors import IRValidationError, LLMResponseError
from smartcut.llm.client import DeepSeekClient
from smartcut.logging import get_logger
from smartcut.orchestrator.context import AgentToolkit
from smartcut.orchestrator.graph import validate_graph
from smartcut.orchestrator.models import OrchestrationPlan, OrchestratorTask
from smartcut.policy import PHASE_NAMES, load_policy

_POLICY = load_policy()

AGENT_CATALOG: dict[str, str] = {
    name: item.description for name, item in _POLICY.agents.agents.items()
}

VISUAL_AGENTS = {"frames", "scenes", "quality", "faces", "contactsheet"}
OUTLINE_FORBIDDEN = set(_POLICY.workflow.phases["outline"].forbidden)
FAST_OUTLINE_AGENTS = set(_POLICY.workflow.phases["fast_outline"].required)
FAST_OUTLINE_FORBIDDEN = set(_POLICY.workflow.phases["fast_outline"].forbidden)
PHASES = PHASE_NAMES

SYSTEM_PROMPT = """你是视频剪辑系统的 Orchestrator 任务规划器。只输出 JSON，不要解释或 Markdown。
根据用户目标与项目状态，构建一个任务图（DAG）。规则：
1. 必须包含 analyze。
2. 有视频轨时必须包含 frames →（scenes、quality、faces、contactsheet）；
   scenes/contactsheet 依赖 frames；quality/faces 依赖 frames 与 scenes
   （场景自适应采样），scenes 之后的三个任务可并行。
3. 有音频时必须包含 transcribe；它可以与 analyze 并行。
4. 有视频轨时必须包含 chapters；chapters 依赖 frames、scenes、quality、faces，
   以及（若有）transcribe。
5. 有视频轨时必须包含 candidates；candidates 依赖 frames、scenes、quality、faces、
   chapters，以及（若有）transcribe。
6. 如果可用规划能力（DeepSeek 或手工 IR），必须包含 plan；
   plan 依赖 analyze、frames、scenes、quality、faces、contactsheet、chapters、candidates，
   以及（若有）transcribe。
7. plan 之后必须依次包含 audio → critic → qa：
   audio 依赖 plan，critic 依赖 audio，qa 依赖 critic。
   可选加入 replan（qa 依赖之后执行），replan 依赖 qa。
8. 只在允许渲染时包含 compile → render → verify 链；
   compile 依赖 replan（若存在）否则依赖 qa，
   render 必须依赖 compile，verify 必须依赖 render。
9. 每个 agent 最多出现一次；depends_on 只能引用已有任务 id；禁止环。
10. 不要添加未列出的 agent；不要输出文件路径或命令。
11. 输出的 tasks 字段必须符合以下 JSON Schema。
12. phase="outline" 时只生成到 chapters：禁止 candidates/plan/critic/compile/render/verify。
    phase="fast_outline" 时只生成 analyze → frames → scenes → contactsheet → chapters，
    禁止 quality/faces/transcribe/candidates/plan 及后续任务。
    phase="plan" 时按完整 DAG 生成。
"""


class TaskPlanner:
    def __init__(
        self,
        toolkit: AgentToolkit,
        settings: Settings,
        llm: DeepSeekClient | None = None,
        logger: Any | None = None,
    ) -> None:
        self.toolkit = toolkit
        self.settings = settings
        self.llm = llm
        self.logger = logger or get_logger("smartcut.orchestrator.planner")

    @staticmethod
    def _condition_enabled(
        condition: str,
        *,
        facts: MediaFacts,
        allow_render: bool,
        can_plan: bool,
    ) -> bool:
        if condition == "always":
            return True
        if condition == "video":
            return facts.video is not None
        if condition == "audio":
            return facts.has_audio
        if condition == "plan":
            return can_plan
        if condition == "render":
            return allow_render and can_plan
        raise ValueError(f"未知任务条件: {condition!r}")

    def allowed_dynamic_agents(
        self,
        project_id: str,
        *,
        allow_render: bool,
        manual_ir: EditIR | None,
        phase: str,
    ) -> frozenset[str]:
        """动态任务只能使用当前 phase 中条件已满足的 agent。"""
        phase_policy = _POLICY.workflow.phases.get(phase)
        if phase_policy is None:
            return frozenset()
        facts = self.toolkit.get_media_facts(project_id)
        can_plan = self.toolkit.planner_available or manual_ir is not None
        return frozenset(
            task.agent
            for task in phase_policy.tasks
            if self._condition_enabled(
                task.when,
                facts=facts,
                allow_render=allow_render,
                can_plan=can_plan,
            )
        )

    def build(
        self,
        project_id: str,
        goal: str,
        *,
        allow_render: bool,
        manual_ir: EditIR | None,
        phase: str = "plan",
        run_id: str | None = None,
        cancel_token: Any | None = None,
    ) -> OrchestrationPlan:
        if phase not in PHASES:
            raise ValueError(f"未知 phase: {phase!r}")
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        facts = self.toolkit.get_media_facts(project_id)
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()

        if phase == "audio_first":
            # 音频优先固定使用规则图：transcribe 必须先于 frames。
            return self._rule_plan(
                facts,
                allow_render=allow_render,
                manual_ir=manual_ir,
                phase=phase,
            )

        if (
            self.settings.orchestrator_use_llm
            and self.llm is not None
            and self.llm.available
        ):
            try:
                plan = self._llm_plan(
                    project_id,
                    goal,
                    facts,
                    allow_render=allow_render,
                    manual_ir=manual_ir,
                    phase=phase,
                    run_id=run_id,
                    cancel_token=cancel_token,
                )
                self._validate_plan(plan, facts, allow_render, manual_ir, phase)
                return plan
            except (KeyError, LLMResponseError, ValueError, IRValidationError) as exc:
                self.logger.warning(
                    "orchestrator_llm_plan_fallback",
                    error=str(exc),
                )

        return self._rule_plan(
            facts,
            allow_render=allow_render,
            manual_ir=manual_ir,
            phase=phase,
        )

    def _rule_plan(
        self,
        facts: MediaFacts,
        *,
        allow_render: bool,
        manual_ir: EditIR | None,
        phase: str = "plan",
    ) -> OrchestrationPlan:
        phase_policy = _POLICY.workflow.phases.get(phase)
        if phase_policy is None or not phase_policy.tasks:
            raise ValueError(f"workflow.json 缺少 phase {phase!r} 的任务图")
        can_plan = self.toolkit.planner_available or manual_ir is not None

        selected = [
            spec
            for spec in phase_policy.tasks
            if self._condition_enabled(
                spec.when,
                facts=facts,
                allow_render=allow_render,
                can_plan=can_plan,
            )
        ]
        selected_ids = {spec.id for spec in selected}
        tasks: list[OrchestratorTask] = []
        for spec in selected:
            missing = [dep for dep in spec.depends_on if dep not in selected_ids]
            if missing:
                raise ValueError(
                    f"phase {phase!r} 的 task {spec.id} 缺少依赖: {missing}"
                )
            depends_on = list(spec.depends_on)
            depends_on.extend(
                dep
                for dep in spec.optional_depends_on
                if dep in selected_ids and dep not in depends_on
            )
            tasks.append(
                OrchestratorTask(
                    id=spec.id,
                    agent=spec.agent,
                    depends_on=depends_on,
                    allow_failed_deps=spec.allow_failed_deps,
                    description=spec.description or None,
                )
            )

        rationale = phase_policy.rationale or f"规则规划：{phase}"
        if not can_plan:
            rationale += "（未配置 DeepSeek/手工 IR，仅执行感知与转写）"
        elif not allow_render:
            rationale += "（不执行渲染）"
        return OrchestrationPlan(rationale=rationale, tasks=tasks)

    def _llm_plan(
        self,
        project_id: str,
        goal: str,
        facts: MediaFacts,
        *,
        allow_render: bool,
        manual_ir: EditIR | None,
        phase: str = "plan",
        run_id: str | None = None,
        cancel_token: Any | None = None,
    ) -> OrchestrationPlan:
        assert self.llm is not None
        schema = OrchestrationPlan.model_json_schema(by_alias=True)
        system = SYSTEM_PROMPT + json.dumps(schema, ensure_ascii=False)
        state = {
            "goal": goal,
            "phase": phase,
            "allow_render": allow_render,
            "manual_ir_available": manual_ir is not None,
            "planner_available": self.toolkit.planner_available or manual_ir is not None,
            "media_facts": {
                "duration_seconds": facts.duration.to_ffmpeg_seconds(),
                "fps": facts.fps,
                "has_audio": facts.has_audio,
                "width": facts.width,
                "height": facts.height,
                "vfr": facts.vfr,
                "rotation": facts.rotation,
            },
            "agents": AGENT_CATALOG,
        }
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(state, ensure_ascii=False)},
        ]
        return self.llm.chat_json(
            messages,
            OrchestrationPlan,
            validate=lambda plan: self._validate_plan(
                plan,
                facts,
                allow_render,
                manual_ir,
                phase,
            ),
            max_repairs=1,
            cancel_token=cancel_token,
            usage_scope=run_id,
            token_budget=self.settings.llm_token_budget_per_run or None,
        )

    def _validate_plan(
        self,
        plan: OrchestrationPlan,
        facts: MediaFacts,
        allow_render: bool,
        manual_ir: EditIR | None,
        phase: str = "plan",
    ) -> None:
        validate_graph(plan.tasks, set(AGENT_CATALOG))
        by_agent = {task.agent: task for task in plan.tasks}
        agents = list(by_agent)
        if len(agents) != len(plan.tasks):
            raise ValueError("每个 agent 最多出现一次")
        if "analyze" not in by_agent:
            raise ValueError("任务图必须包含 analyze")
        if phase == "fast_outline":
            forbidden = set(by_agent) & FAST_OUTLINE_FORBIDDEN
            if forbidden:
                raise ValueError(f"fast_outline 阶段禁止包含: {sorted(forbidden)}")
            if facts.video is not None:
                missing = FAST_OUTLINE_AGENTS - set(by_agent)
                if missing:
                    raise ValueError(f"fast_outline 缺少任务: {sorted(missing)}")
                chapter_task = by_agent["chapters"]
                for name in ("frames", "scenes"):
                    if by_agent[name].id not in chapter_task.depends_on:
                        raise ValueError(f"chapters 必须依赖 {name}")
                if by_agent["frames"].id not in by_agent["contactsheet"].depends_on:
                    raise ValueError("contactsheet 必须依赖 frames")
            return
        if phase == "outline":
            forbidden = set(by_agent) & OUTLINE_FORBIDDEN
            if forbidden:
                raise ValueError(f"outline 阶段禁止包含: {sorted(forbidden)}")
            if facts.video is not None:
                if "frames" not in by_agent:
                    raise ValueError("outline 阶段有视频轨时必须包含 frames")
                if "scenes" not in by_agent or "quality" not in by_agent:
                    raise ValueError("outline 阶段有视频轨时必须包含 scenes 与 quality")
                chapter_task = by_agent.get("chapters")
                if chapter_task is None:
                    raise ValueError("outline 阶段有视频轨时必须包含 chapters")
                if by_agent["frames"].id not in chapter_task.depends_on:
                    raise ValueError("chapters 必须依赖 frames")
                for name in ("scenes", "quality", "faces"):
                    if name not in by_agent:
                        continue
                    if by_agent[name].id not in chapter_task.depends_on:
                        raise ValueError(f"chapters 必须依赖 {name}")
                for name in ("quality", "faces"):
                    if name in by_agent and by_agent["scenes"].id not in by_agent[name].depends_on:
                        raise ValueError(f"{name} 必须依赖 scenes（场景自适应采样）")
                if facts.has_audio:
                    if "transcribe" not in by_agent:
                        raise ValueError("outline 阶段有音频时必须包含 transcribe")
                    if by_agent["transcribe"].id not in chapter_task.depends_on:
                        raise ValueError("chapters 必须依赖 transcribe")
            elif facts.has_audio and "transcribe" not in by_agent:
                raise ValueError("outline 阶段有音频时必须包含 transcribe")
            return

        if (
            phase == "audio_first"
            and facts.has_audio
            and facts.video is not None
        ):
            if (
                "transcribe" in by_agent
                and "frames" in by_agent
                and by_agent["transcribe"].id not in by_agent["frames"].depends_on
            ):
                raise ValueError("audio_first 阶段 frames 必须依赖 transcribe")

        if facts.video is not None:
            if "frames" not in by_agent:
                raise ValueError("有视频轨时必须包含 frames")
            if "scenes" not in by_agent or "quality" not in by_agent:
                raise ValueError("有视频轨时必须包含 scenes 与 quality")
            if "chapters" not in by_agent:
                raise ValueError("有视频轨时必须包含 chapters")
            frames_task = by_agent["frames"]
            for name in ("scenes", "quality", "faces", "contactsheet"):
                task = by_agent.get(name)
                if task is None:
                    continue
                if frames_task.id not in task.depends_on:
                    raise ValueError(f"{name} 必须依赖 frames")
            for name in ("quality", "faces"):
                task = by_agent.get(name)
                if task is not None and by_agent["scenes"].id not in task.depends_on:
                    raise ValueError(f"{name} 必须依赖 scenes（场景自适应采样）")
            chapter_task = by_agent["chapters"]
            for name in ("frames", "scenes", "quality"):
                task = by_agent.get(name)
                if task is None or task.id not in chapter_task.depends_on:
                    raise ValueError(f"chapters 必须依赖 {name}")
            faces_task = by_agent.get("faces")
            if faces_task is not None and faces_task.id not in chapter_task.depends_on:
                raise ValueError("chapters 必须依赖 faces")
            if facts.has_audio:
                transcribe_task = by_agent.get("transcribe")
                if transcribe_task is None:
                    raise ValueError("有音频轨时必须包含 transcribe")
                if transcribe_task.id not in chapter_task.depends_on:
                    raise ValueError("chapters 必须依赖 transcribe")

            if "candidates" not in by_agent:
                raise ValueError("有视频轨时必须包含 candidates")
            candidate_task = by_agent["candidates"]
            for name in ("frames", "scenes", "quality", "chapters"):
                task = by_agent.get(name)
                if task is None or task.id not in candidate_task.depends_on:
                    raise ValueError(f"candidates 必须依赖 {name}")
            if faces_task is not None and faces_task.id not in candidate_task.depends_on:
                raise ValueError("candidates 必须依赖 faces")
            if facts.has_audio:
                transcribe_task = by_agent.get("transcribe")
                if transcribe_task is None:
                    raise ValueError("有音频轨时必须包含 transcribe")
                if transcribe_task.id not in candidate_task.depends_on:
                    raise ValueError("candidates 必须依赖 transcribe")

        can_plan = self.toolkit.planner_available or manual_ir is not None
        if can_plan and "plan" not in by_agent:
            raise ValueError("有规划能力时必须包含 plan")
        if not can_plan and "plan" in by_agent:
            raise ValueError("没有 DeepSeek Key 或手工 IR 时不能包含 plan")
        for name in ("audio", "critic", "qa"):
            if can_plan and name not in by_agent:
                raise ValueError(f"有规划能力时必须包含 {name}")
            if not can_plan and name in by_agent:
                raise ValueError(f"没有规划时不能包含 {name}")
        if "audio" in by_agent and by_agent["plan"].id not in by_agent["audio"].depends_on:
            raise ValueError("audio 必须依赖 plan")
        if "critic" in by_agent and by_agent["audio"].id not in by_agent["critic"].depends_on:
            raise ValueError("critic 必须依赖 audio")
        if "qa" in by_agent and by_agent["critic"].id not in by_agent["qa"].depends_on:
            raise ValueError("qa 必须依赖 critic")
        if "replan" in by_agent:
            if "qa" not in by_agent:
                raise ValueError("包含 replan 时必须包含 qa")
            if by_agent["qa"].id not in by_agent["replan"].depends_on:
                raise ValueError("replan 必须依赖 qa")

        if facts.has_audio and "transcribe" not in by_agent:
            raise ValueError("素材有音频时必须包含 transcribe")

        if "plan" in by_agent:
            required = {"analyze"}
            if facts.video is not None:
                required.update(
                    {
                        "frames",
                        "scenes",
                        "quality",
                        "faces",
                        "contactsheet",
                        "chapters",
                        "candidates",
                    }
                )
            if facts.has_audio:
                required.add("transcribe")
            plan_task = by_agent["plan"]
            for name in required:
                if name not in by_agent:
                    continue
                if by_agent[name].id not in plan_task.depends_on:
                    raise ValueError(f"plan 必须依赖 {name}")

        if allow_render and "plan" in by_agent and "compile" not in by_agent:
            raise ValueError("allow_render=true 且包含 plan 时必须包含 compile")
        compile_dep = "replan" if "replan" in by_agent else "qa"
        if "compile" in by_agent and compile_dep in by_agent:
            if by_agent[compile_dep].id not in by_agent["compile"].depends_on:
                raise ValueError(f"compile 必须依赖 {compile_dep}")

        render_agents = {"compile", "render", "verify"}
        present = render_agents.intersection(by_agent)
        if not allow_render and present:
            raise ValueError("allow_render=false 时不能包含 compile/render/verify")
        if "render" in by_agent:
            compile_task = by_agent.get("compile")
            if compile_task is None:
                raise ValueError("包含 render 时必须包含 compile")
            render_task = by_agent["render"]
            if compile_task.id not in render_task.depends_on:
                raise ValueError("render 必须依赖 compile")
        if "verify" in by_agent:
            render_task = by_agent.get("render")
            if render_task is None:
                raise ValueError("包含 verify 时必须包含 render")
            verify_task = by_agent["verify"]
            if render_task.id not in verify_task.depends_on:
                raise ValueError("verify 必须依赖 render")
