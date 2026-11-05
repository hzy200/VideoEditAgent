# Phase 3：大纲确认、硬件编码与代理优先

## 概览

Phase 3 增加三块能力：

1. 长视频两阶段运行：先出大纲，人工确认后再规划/渲染。
2. 硬件编码自动探测与安全回退；打开项目优先播放代理。
3. 多模态 contact sheet/keyframes 视觉摘要，注入文本 Planner。

并新增机器可读策略文件，防止 agent/tool/encoder/vision 规则漂移。

## 两阶段流程

```
Phase A (phase=outline)
  analyze → frames → {scenes ∥ quality ∥ faces ∥ contactsheet}
  ∥ transcribe → chapters
  → run.status = awaiting_approval（job 仍 succeeded）

UI 大纲确认
  勾选 / 改标题 / 上下移动 / 查看 keyframes
  POST /runs/{run_id}/approve-outline

Phase B (phase=plan, approved_outline_id=...)
  analyze/感知/转写（缓存命中）→ chapters(approved) → candidates(selected)
  → vision notes（可选）→ plan → critic → compile → render → verify
```

- 确认动作创建**新 run**，旧 run 的 ledger 不变。
- 只允许选择已生成章节；start/end 必须与生成大纲一致，标题可改。
- Planner 收到 `chapter_ids` 后跳过 LLM 章节选择，避免二次漂移。
- 候选片段只针对确认章节生成；缓存键包含章节 id。

## 数据模型

`outline_approvals`：

| 列 | 说明 |
|---|---|
| id | `outline_...` |
| project_id / run_id | 来源 |
| source_hash | 源文件内容哈希 |
| goal | 原始目标 |
| chapters_json | 确认后的章节快照（含标题/顺序/keyframes） |
| notes | 用户备注 |
| created_at | 时间 |

`orchestration_runs` 新增：`phase`（outline/plan）、`approved_outline_id`。
`Database.init()` 通过 `PRAGMA table_info` + `ALTER TABLE` 对旧库补列。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/projects/{id}/orchestrate` | 增加 `phase` 字段 |
| GET | `/api/runs/{run_id}/outline` | 获取生成/确认大纲 |
| POST | `/api/runs/{run_id}/approve-outline` | 提交确认章节，创建 Phase B run |
| GET | `/api/projects/{id}/candidates/search?q=` | 候选检索（Phase 2） |
| GET | `/api/projects/{id}/revisions/{rev}/diff/{base}` | revision diff（Phase 2） |

## 桌面 UI

- 新增“大纲确认”标签页：可勾选、双击改标题、上移/下移、查看 keyframe 并 seek。
- AI 面板新增“先生成大纲（人工确认后继续）”。
- 运行结束状态 `awaiting_approval` 自动切到大纲页。
- 项目打开时优先载入最新 proxy；代理生成在后台继续。
- 感知页新增章节关键帧缩略图。
- 设置页新增硬件编码（auto/off）与视觉 Base URL/模型/Key。

## 策略与文档

- `src/smartcut/policies/{agents,tools,encoding,vision,workflow}.json`
- `src/smartcut/policy.py` 加载与一致性校验；`SMARTCUT_POLICY_DIR` 可覆盖。
- `scripts/check_drift.py` 与 `tests/test_policy_drift.py` 强制代码/策略一致。
- 相关文档：`INVARIANTS.md`、`AGENT_PROTOCOL.md`、`MEDIA_PIPELINE.md`、`VISION.md`。

## 验收

- Phase A 在 chapters 后停止，无 revision/渲染。
- 确认章节后 Phase B 只使用选中章节，且不再次做章节选择。
- 硬件编码不可用/失败时回退 libx264，并记录 actual_encoder。
- proxy 优先加载；代理失败不影响分析与渲染。
- VLM 未配置时行为与 Phase 2 完全一致；配置后失败可降级。
- 旧数据库可无损迁移；全量测试与打包自检通过。

## 后续语义/证据增强（已实现）

P0–P2 的视图路由/多视图、测量事实与 `visual_facts`、屏幕文字 OCR、主体跟踪、
相机/主体运动拆分、镜头聚类、统一指纹、批量渲染与 GpuMeasurer，见
`README.md`、`docs/VISION.md`、`docs/INVARIANTS.md`（91–103）与
`docs/PERFORMANCE.md`。
