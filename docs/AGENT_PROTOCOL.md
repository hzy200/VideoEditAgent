# Agent 协议与阶段门

## 子智能体契约

- 每个 agent 必须实现 `name`、`description`、`run(task, ctx) -> AgentResult`。
- Agent 必须无状态、线程安全；不得在 `run()` 内起子进程或直接读写媒体。
- 业务能力只能通过 `AgentToolkit`（`orchestrator/context.py`）调用。
- 动态任务只能通过 `AgentResult.new_tasks` 返回；依赖必须已存在，禁止环。
- 失败应尽量返回 warning + 降级结果；会阻断后续的阶段才抛异常。
- `AgentResult.confidence` 限制在 0..1；警告会进入最终答案。

## Phase A：outline

```
analyze → frames → {scenes ∥ quality ∥ faces ∥ contactsheet}
transcribe（有音频）→ chapters
```

- 禁止 `candidates/plan/critic/compile/render/verify`（见 `workflow.json`）。
- 全部成功后 run.status=`awaiting_approval`，job 状态仍为 succeeded。
- 章节结果来自缓存/感知，不产生 EditIR revision。

## Phase B：plan

```
analyze/感知/转写（缓存命中）→ chapters(approved) → candidates(selected)
→ plan → audio → critic → qa → compile → render → verify
```

- `approved_outline_id` 指向 `outline_approvals` 快照。
- `ChapterizerAgent` 直接返回确认章节，不重新切分。
- `CandidateAgent` 只针对确认章节生成候选。
- `PlanAgent` 传入 `chapter_ids`，`DeepSeekPlanner` 跳过 LLM 章节选择。
- `pinned_effects` 非空时，Plan/Replan 必须把该数组强制写入新 revision；
  与 `manual_ir` 互斥；锁定项与新时间线不兼容时整次运行失败，不静默丢弃。
- 逐片段锁定特效按源区间派生的稳定 `segment_id` 匹配；AI 改动对应片段边界时
  视为不兼容并失败，而不是把特效静默改绑或丢弃。
- 规划片段的 `allowed_ranges` 必须**完整包含** trim，而不是仅与章节有重叠；
  相邻/重叠的确认章节会先合并。
- `approve_outline` 允许人工调整章节 start/end（必须在素材时长内、按顺序且不重叠），
  调整后的边界会进入 Phase B 的 `chapter_ids` 与 allowed_ranges。
- 视觉摘要在这里按需生成；失败只产生 warning，不改变任务图。
- `AudioAgent` 在 `audio.remove_filler=true` 时按词级时间戳删口癖并提交新 revision。
- `CriticAgent` 在 Audio 之后做质量/长片段修复。
- `SemanticQAAgent` 在 Critic 之后审片，写入 `SharedState.review`，不修改时间线；
  LLM 不可用或失败时使用确定性规则审片。
- `compile` 依赖 `qa`；QA 的 score 会限制最终置信度。

## 失败语义

| 情况 | 行为 |
|---|---|
| 感知 agent 失败 | 返回 warning，Plan 使用可用上下文 |
| 转写降级 | 记 `degraded`，仍可规划/渲染 |
| 候选生成失败 | Plan 直接读取转写 |
| Critic 失败 | 保留原规划，告警 |
| 视觉失败 | 纯文本规划，告警 |
| 硬件编码失败 | 当前调用回退 libx264；verify 失败则软件重渲一次 |
| 取消 | 任务/子进程 kill；已完成缓存保留，可新建 run 续跑 |

## 规则任务图与 LLM 边界

- 默认任务图来自 `policies/workflow.json` 的 `tasks`（`when` 条件 + 必选/可选依赖），
  `TaskPlanner._rule_plan` 只做条件求值与依赖过滤。
- `orchestrator_use_llm=false`（默认）时不会让 LLM 生成 DAG。
- LLM 只用于高价值决策：章节选择、EditIR 规划、语义/成片审片、可选最终总结。
- 只有显式开启 `orchestrator_use_llm` 才走 LLM DAG 路径，并仍受 `_validate_plan` 约束。

## SharedState 与账本

- `AgentContext.shared` 返回冻结快照；dict 字段为只读视图，agent 只能通过
  `set_*` / `add_warning` 更新，禁止直接赋值或原地修改。
- 每次更新以 `dataclasses.replace` 原子替换快照，并行 agent 读到一致视图。
- 任务图只向 `on_task_complete` 回调新增终态；Services 按 `(run_id, task_id)`
  upsert `orchestration_task_outcomes`，最终再写一次完整 `ledger_json` 快照。
- 进程中断后可从增量表恢复账本；完整快照仍用于列表/详情读取。

## 任务 id 规则

- 安全字符：字母、数字、下划线、连字符，1-64 字符。
- 每个 agent 最多出现一次；依赖必须存在；禁止环。
- 规则图与 LLM 图都要经过 `TaskPlanner._validate_plan`。
- 阶段限制由 `src/smartcut/policies/workflow.json` 驱动，策略漂移由测试拦截。

## 输出规则

- Agent 不得输出 ffmpeg 命令、绝对路径、crop 坐标、字幕输出时间。
- 所有结构化输出通过 Pydantic 模型；模型修复次数受限。
- 最终答案由 `DecisionAgent` 基于账本确定性生成；可选 LLM 总结。
