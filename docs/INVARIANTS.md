# SmartCut 不变量与强制点

本文件列出不可协商的工程规则，以及它们在代码、策略和测试中的落点。
修改代码时必须同步更新对应测试；`scripts/check_drift.py` 会检查策略漂移。

| # | 不变量 | 强制点 |
|---|---|---|
| 1 | 源文件只读，所有处理在项目工作区副本上 | `server/services/facade.py` ingest/upload；工作区测试 |
| 2 | 所有工具路径必须是 workspace 相对路径，拒绝绝对路径/`..`/NUL/符号链接逃逸 | `runner/paths.py`；`tests/test_contracts.py` |
| 3 | 只有 Runner 能起子进程，`shell=False` + 白名单工具 | `runner/execute.py`；`runner/tools/registry.py` |
| 4 | 时间只用 `RationalTime`，IR/工具边界才转十进制 | `contracts/time.py`；`tests/test_contracts.py` |
| 5 | AI 只输出 `EditIR`，不输出路径、命令、坐标或字幕时间 | `planner/`、`compiler/`；Planner 校验 |
| 6 | Compiler 是纯函数，不做隐式调整 | `compiler/lower.py`、`compiler/encoding.py` |
| 7 | effects 不得改变时长 | `contracts/edit_ir.py` |
| 8 | 所有昂贵产物走内容寻址缓存（source_hash + tool + params） | `server/cache.py`；`tests/test_cache.py` |
| 9 | 长任务检查 `CancellationToken`，取消保留已完成缓存 | `cancel.py`、`runner/execute.py`；`tests/test_cancel.py` |
| 10 | ffmpeg/CPU/LLM 并发受 `ResourceManager` 限制 | `resources.py`；`tests/test_resume.py` 等 |
| 11 | 感知结果只写 JSON/JPG，模型不接触像素坐标 | `perception/`；`compiler/fit.py` |
| 12 | fit/subtitles/loudnorm 必须走 Compiler + Runner | `compiler/lower.py`、`runner/tools/` |
| 13 | LLM 输出必须经过 Pydantic 校验与有限修复 | `llm/client.py`、`planner/client.py` |
| 14 | 生产/测试默认关闭硬件编码时可完全复现 libx264 argv | `compiler/encoding.py`；`tests/test_compiler.py`、`test_encoders.py` |
| 15 | 硬件编码失败必须回退 libx264，不得让渲染静默失败 | `server/services/rendering.py::_execute_with_fallback`；`tests/test_encoders.py` |
| 16 | Segment 缓存键不包含输出路径；跨 revision 只复用同裁剪参数 | `server/services/rendering.py::_cut_cache_key`；`tests/test_segment_cache.py` |
| 17 | 并行 cut 受 `ffmpeg_concurrency` 限制，结果按 seq 排序 | `server/services/rendering.py::_render_impl` |
| 18 | 大纲确认必须创建新 run，不修改旧 run 的 ledger | `server/services/orchestration.py::approve_outline`；`tests/test_outline_flow.py` |
| 19 | Phase B 使用人工确认章节，Planner 不得再次做章节选择 | `planner/client.py::propose`；`tests/test_outline_flow.py` |
| 20 | 视觉输入只发送项目内图片，未配置 VLM 时完全禁用 | `planner/vision.py`、`llm/vision.py`；`tests/test_vision.py` |
| 21 | 视觉失败必须降级为纯文本规划，不阻断渲染 | `server/services/facade.py::plan`；`tests/test_vision.py` |
| 22 | agent/tool/encoder/phase 必须与 `src/smartcut/policies/*.json` 一致；phase 名称以 `contracts.templates.Phase` 为类型源，`workflow.json` 为任务图唯一源（`agents.json` 不再保存 phase 成员表） | `policy.py::validate_consistency`；`tests/test_policy_drift.py`；`scripts/check_drift.py` |
| 23 | Agent 只能通过 `AgentToolkit` 访问业务能力，不得直接起进程/操作媒体 | `orchestrator/context.py`；AGENTS.md |
| 24 | 桌面 QWidget 不直接调用 Services/subprocess/httpx | `desktop/controller.py`；桌面测试 |
| 25 | source_hash 在 ingest 写入 DB，后续不得重复全量哈希 | `server/services/facade.py::_register_asset`、`server/services/base.py::_source_hash`；`tests/test_cache.py` |
| 26 | 项目 bundle 独立查询并行；运行列表不得返回完整 ledger/plan | `desktop/controller.py`、`list_orchestration_run_summaries` |
| 27 | fit+字幕+响度合并为一次 `postprocess`；仅响度时视频 copy | `compiler/lower.py`、`runner/tools/postprocess.py` |
| 28 | 任务图必须依赖驱动调度，短分支不得等待长并行任务 | `orchestrator/graph.py`；`tests/test_orchestrator_graph.py` |
| 29 | 感知结果按缓存签名在内存复用，避免重复读 JSON | `services.get_perception`；`tests/test_cache.py` |
| 30 | 视觉摘要先按 source/章节/图片路径查缓存，再决定是否读图 | `services.build_visual_notes` |
| 31 | AudioAgent 只按词级时间戳局部删口癖，必须提交新 revision | `orchestrator/audio.py`、`agents/audio.py`；`tests/test_audio_qa.py` |
| 32 | SemanticQAAgent 只审片不改时间线；失败降级规则审片 | `agents/qa.py`、`services.review_edit`；`tests/test_audio_qa.py` |
| 33 | 视觉摘要双层缓存：整图路径键优先，单图 sha256 只补缺失图 | `services.build_visual_notes`；`tests/test_vision.py` |
| 34 | 硬件编码器必须通过 64x64 试编码才算可用 | `runner/encoders.py`；`tests/test_encoders.py` |
| 35 | 质量/人脸按帧并行，逐帧 `cpu_slot`，Cascade thread-local | `perception/quality.py`、`faces.py`；`tests/test_perception.py` |
| 36 | 感知分析受 max_frames 限制，优先镜头切点附近帧 | `perception/frames.py::select_analysis_frames`；`tests/test_perception.py` |
| 37 | quality/faces 依赖 scenes，缓存键包含 scenes key 与 max_frames | `orchestrator/planner.py`、`services.analyze_quality/detect_faces` |
| 38 | 运行列表只返回摘要与计数，不返回 ledger/plan | `services._run_summary_dict`；`tests/test_orchestrator_e2e.py` |
| 39 | 每次 run 持久化 metrics（agent 耗时/队列/重试/缓存/token/encoder） | `orchestrator.py::_collect_metrics`、`metrics_json`；`tests/test_orchestrator_e2e.py` |
| 40 | LLM 客户端记录 usage 与缓存命中，不记录明文 prompt/Key | `llm/client.py`；`tests/test_planner.py` |
| 41 | FTS top 在 SQLite 内按 score 排序并缓存，索引变更失效缓存 | `server/candidate_index.py`；`tests/test_candidates.py` |
| 42 | 工具审计批量写，任务结束/失败/取消时 flush | `server/jobs.py`；`tests/test_jobs.py` |
| 43 | 时间线拖拽/边缘裁剪/增删/分割/排序只在本地编辑，保存时生成新 revision | `desktop/widgets/timeline_panel.py`；`tests/test_desktop_gui.py` |
| 44 | 所有时间线编辑必须支持 Undo/Redo；非法区间自动回滚 | `timeline_panel._commit/_validate`；GUI 测试 |
| 45 | 选中片段循环预览；空格/左右逐帧/Delete/Ctrl+Z 不吞文本输入 | `desktop/widgets/preview_panel.py`、`main_window.eventFilter` |
| 46 | 候选片段通过 Controller 检索，一键加入时间线 | `candidate_panel.py`、`controller.search_candidates`；GUI 测试 |
| 47 | revision diff 同时在 Inspector 文本与时间线高亮 | `inspector_panel`、`timeline_widget.set_diff`；GUI 测试 |
| 48 | Run metrics 在 GUI 展示 agent 队列/耗时/重试/缓存/token/编码器 | `metrics_panel.py`、`main_window._on_run_finished`；GUI 测试 |
| 49 | 时间线只绘制可见区间；缩放/平移使用统一 time↔x 换算 | `timeline_widget.py`；GUI 测试 |
| 50 | 波形 peaks 走内容寻址缓存且可复用 | `services.build_waveform`；`tests/test_cache.py` |
| 51 | 缩略图异步解码 + LRU QPixmap 缓存，不在 UI 线程批量读图 | `widgets/thumbnails.py`；GUI 测试 |
| 52 | 字幕预览与烧录共用字号/颜色/位置；ASS 颜色在 Compiler 转换 | `compiler/lower.py`、`runner/tools/postprocess.py`；编译器测试 |
| 53 | 长视频/字幕/导出参数可从设置页修改并持久化 | `settings_dialog.py`、`settings_store.py`；GUI 测试 |
| 54 | 项目删除必须级联清库/索引/工作区，且拒绝仍有活动任务的项目 | `services.delete_project`；`tests/test_project_admin.py` |
| 55 | 未保存时间线在切换项目/退出前必须确认；保存完成后再执行原动作 | `main_window._resolve_unsaved`、`_pending_after_save`；GUI 测试 |
| 56 | 窗口几何/状态/分割条/标签页/最近项目持久化并可恢复 | `main_window._capture/_restore_window_state`、`settings_store.py`；GUI 测试 |
| 57 | 桌面任务进度只用 Qt 信号；SSE/WebSocket 不是桌面依赖 | `desktop/controller.py`；`tests/test_project_admin.py` |
| 58 | 主题/缩放/高对比度只影响 QApplication 样式，不改变业务数据 | `desktop/theme.py`；GUI 测试 |
| 59 | 手动字幕写入 `EditIR.subtitles`；None=自动，[]=无字幕，非空=手动 | `contracts/edit_ir.py`、`compiler/captions.py`；字幕测试 |
| 60 | 全局音量/音调只通过 Compiler + 单次 postprocess 应用，音调保持时长 | `compiler/lower.py`、`runner/tools/postprocess.py`；音频测试 |
| 61 | 预览音量/静音只影响监听，不写入成片；音量/音调以 IR 为准 | `preview_panel.py`、`main_window._capture_window_state`；GUI 测试 |
| 62 | 多轨只通过 `EditIR.audio.tracks` 表达；None=旧单轨，[]=静音 | `contracts/audio.py`、`compiler/lower.py`；多轨测试 |
| 63 | 多轨混音必须由 Compiler 生成一次 `mix_audio`，视频流 copy | `runner/tools/mix_audio.py`、`policies/tools.json`；argv/e2e 测试 |
| 64 | Demucs 只能通过外部进程适配器调用，不 import torch，结果进缓存 | `runner/separation.py`、`services.separate_audio`；分离测试 |
| 65 | 循环音轨只在输入层 `-stream_loop`，渲染仍单次音频编码 | `runner/tools/mix_audio.py`；多轨测试 |
| 66 | 混音预览必须防抖、按资产哈希缓存，且不替代最终渲染 | `services.build_preview_mix`、`main_window._schedule_preview_mix`；预览测试 |
| 67 | 音频 lane 波形按资产内容寻址缓存，不能只按路径复用 | `services.build_audio_waveforms`、`timeline_widget.set_audio_waveforms`；GUI/服务测试 |
| 68 | 分离器必须实现 `Separator` 协议；主程序不 import torch | `runner/separation.py`、`tests/test_separation.py` |
| 69 | 包间依赖必须落在 `ALLOWED_EDGES` 白名单内；QWidget 不得 import server/runner/subprocess/httpx | `tests/test_import_boundaries.py`；AGENTS.md 分层铁律 |
| 70 | 时间辅助/RationalTime 解析只能有一份实现；宽泛异常不得静默 `pass`；配置字段必须有生产引用 | `contracts/time.py`、`desktop/formatting.py`；`tests/test_time_helpers.py`、`test_exception_hygiene.py`、`test_config_usage.py` |
| 71 | 转场/变速改变时长，必须走 `transitions`/`speed` 与时长模型，不得放进 effects | `contracts/edit_ir.py`、`compiler/lower.py`；`tests/test_timeline_ops.py` |
| 72 | 有转场时必须用 `compose` 的 xfade/acrossfade；copy 模式拒绝转场/变速/水印 | `runner/tools/compose.py`、`compiler/validate.py`；compose e2e |
| 73 | 水印由 postprocess 生成 drawtext/overlay；字幕时间必须按 speed 与转场重叠重映射 | `runner/tools/postprocess.py`、`compiler/captions.py`；字幕/水印测试 |
| 74 | 图片/Logo 水印必须登记为项目 overlay 资产，IR 只引用 asset_id；路径不得进入 IR | `server/services`、`contracts/overlay_assets.py`；水印测试 |
| 75 | 去水印只允许 GUI 手动矩形坐标，经 `delogo` 应用；模型不得输出像素坐标 | `contracts/timeline_ops.py`、`runner/tools/postprocess.py`；水印测试 |
| 76 | 项目删除与 Job 提交/创建 run 必须共用项目生命周期锁；删除期间/删除后提交立即失败，不得重建工作区 | `server/jobs.py::project_guard/project_lifecycle`、`services.delete_project`；`tests/test_project_admin.py` |
| 77 | JobService 必须有活动/排队上限；取消排队任务即使状态写库失败也必须执行 cancel hook 并清理 token/future | `server/jobs.py::submit/cancel`；`tests/test_jobs.py` |
| 78 | 后台任务完成/失败回调不得调用模态 `exec()`；离屏环境必须抑制确认/信息框 | `desktop/main_window.py::_headless_ui`、非模态通知；`tests/test_desktop_effects.py` |
| 79 | whisper 模型只能在独立 worker 进程加载；崩溃/超时按配置重启重试，取消不重试 | `perception/transcribe.py::ProcessWhisperTranscriber`；`tests/test_chunking.py` |
| 80 | OpenCV 进程池必须有取消/超时 watchdog；触发时 terminate 整个池，不得无限等待单帧 | `perception/process_pool.py`；`tests/test_process_pool.py` |
| 81 | 字幕、manifest、检查点、结果快照、缓存 JSON 与工具 `dst` 必须临时文件 + 原子替换；抽帧目录成功后才整体替换 | `server/atomic.py`、`perception/frames.py`、`runner/execute.py`；缓存/frames 测试 |
| 82 | `ArtifactCache` 哈希缓存与项目缓存注册表必须加锁，避免并发重复哈希/多实例 | `server/cache.py`、`services/base.py::_cache_for`；`tests/test_cache.py` |
| 83 | 无 Key 时规则规划必须标注 `source=rule`，不得冒充 AI；重规划无变化不得新建 revision | `planner/hybrid.py`、`services.plan/replan`；`tests/test_rule_planner.py` |
| 84 | 用户删除片段必须写 `segment_reject`；偏好统计排除被拒标签，拆分/裁剪不得误判为拒绝 | `desktop/main_window._removed_segments`、`services.record_segment_reject/get_edit_preference`；`tests/test_preference.py` |
| 85 | 平台导出预设只映射画幅（reels/douyin 1080x1920、youtube 1920x1080）；copy 模式必须拒绝需要裁切的预设 | `contracts/export_presets.py`、`compiler/validate.py`、`compiler/lower.py`；`tests/test_compiler.py` |
| 86 | VisualEvidence 只作候选证据；renderer 只接收 MeasureResult，不得访问原始素材或写 IR/时间线 | `contracts/visual_evidence.py`、`perception/view/`；`tests/test_evidence_render_cpu.py` |
| 87 | `render_hash` 排除 backend/driver/耗时；meta 必填常量缺失必须拒绝；证据缓存键包含 policy/渲染常量 | `perception/view/synthesizer.py`、`contracts/visual_evidence.py`、`services/evidence.py`；`tests/test_visual_evidence_contract.py` |
| 88 | VLM/Planner 的 `evidence_refs` 只能引用真实 Txx；未知标签必须拒绝，不得静默采信 | `contracts/vision.py`、`planner/client.py::_check_evidence_refs`、`services/visual.py`；`tests/test_evidence_citations.py` |
| 89 | GL 渲染器只做拼版/编号/叠层，不得计算光流或访问源素材；每线程独立 context，失败必须回退 CPU 并记录 backend | `desktop/view/render_qtgl.py`、`services/evidence.py::_synthesize_evidence`；`tests/test_gl_renderer.py`、`tests/test_evidence_service.py` |
| 90 | M2 视图（帧差/光流条带/bbox/ROI/codecview）只作候选证据；codecview 不承诺数值级 MV；ROI 必须回原素材取高分辨率；UI 叠层只读 | `perception/view/flow.py`、`views.py`、`services/evidence.py`、`desktop/widgets/evidence_panel.py`；`tests/test_evidence_views.py` |

## 新增功能的检查清单

1. 契约先落到 `contracts/`，并加入 `contracts/generate.MODELS`。
2. 新 agent/tool/encoder 同步更新 `src/smartcut/policies/*.json`。
3. 新增设置写入 `config.py` 和 `.env.example`，必要时同步 `DesktopConfig`。
4. 涉及数据库的改动必须提供只增不删的迁移与旧库测试。
5. 运行 `python scripts/check_drift.py` 与全量 `pytest`。
