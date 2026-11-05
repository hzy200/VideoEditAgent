# AGENTS.md — SmartCut 工程纪律

本文件供 AI coding agent 每轮必读。架构依据见 `智能剪辑系统技术方案.md`
与 `docs/需求分析与可行性分析.md`；强制规则见 `docs/INVARIANTS.md`，
agent 阶段门见 `docs/AGENT_PROTOCOL.md`，编码/代理见 `docs/MEDIA_PIPELINE.md`，
视觉配置见 `docs/VISION.md`，性能/调度约束见 `docs/PERFORMANCE.md`，
GUI 编辑/快捷键见 `docs/GUI.md`。

## 环境

```bash
conda activate video_edit
python -m pip install -e ".[dev]"
python -m pytest -q

# 桌面开发/构建
python -m pip install -e ".[desktop,transcribe,vision,packaging]"
python -m smartcut.desktop.app
python scripts/prepare_vendor.py --source modelscope
python -m PyInstaller packaging/smartcut.spec --noconfirm --clean
dist\SmartCut\SmartCut.exe --smoke-test --data-dir %TEMP%\smartcut-smoke
dist\SmartCut\SmartCut.exe --self-check --data-dir %TEMP%\smartcut-smoke

# 可选分离伴生 worker（GPU 构建先装 cu126 torch 与 demucs）
python scripts/prepare_demucs_models.py
python -m PyInstaller packaging/smartcut-separator.spec --noconfirm --clean
# 复制 dist\SmartCut-Separator 到 dist\SmartCut\separator\

# Phase 1 专项测试
python -m pytest -q tests/test_cache.py tests/test_chunking.py tests/test_chapters.py tests/test_cancel.py tests/test_resume.py

# Phase 2 专项测试
python -m pytest -q tests/test_candidates.py tests/test_critic.py tests/test_segment_cache.py

# Phase 3 专项测试 + 策略漂移
python -m pytest -q tests/test_outline_contracts.py tests/test_outline_flow.py tests/test_encoders.py tests/test_vision.py tests/test_policy_drift.py
python scripts/check_drift.py

# Phase 4 性能/Agent 专项
python -m pytest -q tests/test_audio_qa.py tests/test_orchestrator_graph.py tests/test_cache.py tests/test_compiler.py

# GUI P2 项目运维/任务/诊断专项
python -m pytest -q tests/test_project_admin.py tests/test_desktop_gui.py tests/test_desktop_smoke.py

# GUI P3 播放/字幕/音频专项
python -m pytest -q tests/test_audio_controls.py tests/test_captions.py tests/test_contracts.py tests/test_desktop_gui.py

# 多轨混音 / 音频资产 / Demucs 适配器
python -m pytest -q tests/test_audio_tracks.py tests/test_audio_assets.py tests/test_separation.py
```

- Python 3.12（不使用 3.14，避开 OpenTimelineIO binding 问题）。
- 第三方库统一用 **pip** 安装；ffmpeg/ffprobe 为系统二进制，通过配置注入。
- 重依赖（faster-whisper / OpenCV / PySceneDetect / OTIO）为 optional extras，
  只能在函数内部懒加载，不得出现在模块顶层 import。
- 桌面包不安装/不导入 FastAPI、uvicorn；server 是可选 extra。
- 打包资源（ffmpeg、whisper 模型）通过 `desktop/paths.py` 解析，
  禁止硬编码 exe 目录或依赖系统 PATH。
- 本地转写必须走 `extract_audio` 生成的 16k 单声道 WAV，再以 numpy 数组
  传给 faster-whisper；不要直接传任意媒体路径（PyAV 19 与该版本
  faster-whisper 的 `metadata_errors` 不兼容）。
- OpenCV 锁定 4.x：5.x 移除了内置 Haar cascade。视觉依赖是 optional extra，
  但桌面构建必须安装并内嵌，self-check 要验证 cv2/Haar/scenedetect。

## 缓存 / 长视频 / 并发纪律

- 所有昂贵产物必须走内容寻址缓存：`source_hash + tool + params_hash`；
  源文件变化必须产生新键，禁止用固定路径冒充缓存。
- 媒体 artifact 缓存按项目工作区隔离（`cache/...`）；LLM 响应缓存可全局共享，
  但键必须包含 model/base_url/messages/temperature/json_mode。
- 长音频必须分块转写；每块单独缓存，合并时按源时间偏移并去重。
- 长视频必须有 `ChapterPlan`；Planner 优先使用章节摘要，只对选中章节做细粒度规划。
- 所有 ffmpeg/CPU/LLM 调用必须通过 `ResourceManager` 的
  `ffmpeg_slot` / `cpu_slot` / `llm_slot`，禁止绕开并发上限。
- 长任务必须检查 `CancellationToken`：子进程由 `ProcessRunner` 轮询取消，
  转写按块检查，感知在关键点检查；取消后保留已完成缓存。
- Orchestrator 必须把任务账本检查点写入 `orchestration_runs.ledger_json`；
  “续跑”是缓存辅助的新 run，不是篡改源文件或复用半成品成片。
- 候选片段索引按 `project_id + source_hash` 隔离；FTS5 不可用时必须回退
  LIKE，禁止因为索引缺失阻断规划。
- Critic 只能做局部结构修正（质量区间/长片段拆分），必须提交**新 revision**，
  不得原地改写旧 revision，也不得绕过 Compiler 直接生成工具调用。
- Segment 缓存键不得包含 `dst`；并行 cut 必须受 `ffmpeg_concurrency` 约束，
  结果按 seq 重排后再写回渲染结果。
- 两阶段大纲：Phase A 只到 chapters，Phase B 必须新建 run 并引用
  `outline_approvals`；禁止原地修改旧 run 的 ledger 或转写/章节缓存。
- Phase B 必须把人工确认的 `chapter_ids` 传给 Planner，跳过 LLM 章节选择。
- 硬件编码：探测只通过 Runner；失败必须回退 libx264 并记录
  `actual_encoder`；默认 libx264 argv 必须与旧 golden 一致。
- 视觉摘要：只有显式配置 VLM 才发送图片；必须限制图片数量/尺寸/字节，
  失败降级为文本规划，不得阻断渲染。
- 多视图证据必须为每个视图分配不相交的 `Txx` 区间；`evidence_refs` 白名单按
  本次全部 TileMap 的并集校验，但跨视图同名标签不得视为合法。
- 证据视图路由必须来自 `policies/vision.json` 的 `routing`；kind 必须属于
  `ViewKind`，default 必须回退 `contact_sheet`，路由版本必须进入缓存键。
- 同一次规划的多视图必须复用同一候选窗口与原始帧抽帧；仅 `roi_crop` 高分辨率
  与 `codec_residual` 允许单独抽帧。
- Measurement 只作为 prompt 事实（d_hist/flow/face/risk）；缺失字段不得补造，
  首帧 `d_hist=1.0` 占位值不得注入。
- 二次复核必须对 flagged 章节构建 scoped 新视图（不同 kind + ROI），不得重复
  发送主视图；ROI 必须按 measurements.score 选最异常时刻（时间定位），不得
  固定 tile 1 或用 dedupe 后的索引直接定位原始帧。
- Planner 的 `visual_facts` 只含章节级数值（view_kinds/face_coverage/
  mean_d_hist/motion_level/quality_risks），不得含坐标或路径；默认限制前 40 章，
  并优先按 `chapter_ids` 过滤。
- 屏幕文字只作候选事实：OCR 重依赖只能在 `ocr_worker.py` 进程内加载，主进程
  禁止 import rapidocr/pytesseract；缓存键必须含 source_hash+frame_sha256+
  worker_version+policy_version；不得写 IR。
- 人脸跟踪必须输出 `track_id` 与出现区间，并对同一 track 平滑锚点；跟踪/平滑
  参数必须进入 faces 缓存键。
- 相机/主体运动必须由 RANSAC 仿射拆分；camera/subject/motion 只作测量事实，
  只在 flow 视图产生，不得写 IR。
- 镜头聚类只允许在视觉簇边界处切章；聚类版本与阈值必须进入 chapters 缓存键。
- dHash/直方图必须来自 `perception/view/fingerprint.py`；版本与 engine 必须进入
  证据/质量/候选缓存键，cv2 缺失时保留 Pillow fallback。
- `render_many` 只做拼版/编号/bbox；一次 context + 解码图片缓存，禁止在
  shader/渲染器内计算光流或直方图。
- GpuMeasurer 必须与 renderer 解耦，只输出数值 MeasureResult；必须有 CPU 等价
  路径与容差比对，失败回退 CPU 并记录 `measure_backend`；该字段不得进入
  `render_hash`。
- `src/smartcut/policies/*.json` 与代码同步；新增 agent/tool/encoder 时
  必须更新策略并运行 `scripts/check_drift.py`。
- 新增数据库列/表必须提供只增不删的迁移与旧库测试。
- `source_hash` 必须在 ingest 写入 `media_assets`；后续服务不得重复全量哈希。
- 渲染后处理必须走单个 `postprocess`；不得重新串行 fit→subtitles→loudnorm。
- TaskGraph 必须依赖驱动调度；禁止恢复“整批 future 完成后才进入下一轮”的屏障。
- 感知结果与视觉摘要必须先查缓存再读盘/读图；缓存键必须包含签名而非仅路径。
- AudioAgent 只做词级口癖局部修复并提交新 revision；不得在 Compiler 静默删音频。
- SemanticQAAgent 只审片不改 IR；LLM 失败必须降级规则审片。
- 项目 bundle 的独立查询应并行；运行列表接口只返回摘要。
- 分块转写：优先跳过低 RMS 静音块；并行仅在 transcriber 支持 `clone()` 或
  明确 `thread_safe` 时启用，GPU 默认 1 worker；缓存写入留在主线程。
- 不得让多个 worker 共享同一个 CTranslate2 模型，也不得绕开 `cpu_slot`。
- 视觉摘要必须双层缓存：整图路径键优先命中；未命中时按单图 sha256 只补缺失图。
- 视觉证据层只能产出候选证据：`VisualEvidence` 不得写 IR/时间线；renderer 只接收
  `MeasureResult`，不得访问原始素材路径；PTS/frame_no 与哈希必须可复现。
- 证据缓存键必须包含 source_hash、policy_version 与渲染常量；`render_hash` 不得
  包含 backend/driver/耗时，CPU/GL 只比较 `semantic_hash`。
- 硬件编码器必须做一次极小试编码；`-encoders` 列表不能作为可用性结论。
- 质量/人脸按帧并行时必须逐帧获取 `cpu_slot`；Haar Cascade 使用 thread-local。
- 质量/人脸分析受 `SMARTCUT_PERCEPTION_MAX_FRAMES` 限制，并优先保留镜头切点帧。
- 每次 Orchestration run 必须持久化 metrics：按 agent 的 queue_wait/duration/
  retries/cache/tokens/encoder；新增 agent 需在 output 中返回可聚合字段。
- LLM 客户端必须记录 usage（含缓存命中）；不得在日志中记录 prompt/Key 明文。
- FTS top 必须在 SQLite 内按 score 排序并缓存；重建索引必须失效缓存。
- 删除项目必须级联清理任务/运行/版本/素材/产物/索引/工作区，且拒绝仍有
  queued/running 任务的项目；不得留下悬挂记录。
- 项目删除与 Job 提交/创建 run 必须共用项目生命周期锁；删除开始后新提交
  立即失败，删除成功后不得再重建工作区。
- `JobService` 必须有活动/排队上限（`SMARTCUT_MAX_PENDING_JOBS`）；队列满
  快速失败，不得无限堆积。
- 后台任务完成/失败回调禁止调用 `QMessageBox.exec()`/`question()` 或
  `QDialog.exec()`；离屏环境必须抑制模态框，统一走状态栏/诊断日志/非模态提示。
- whisper 模型只能在独立 worker 进程加载；崩溃/超时按
  `SMARTCUT_WHISPER_WORKER_RETRIES` 受控重启，取消不重试。
- OpenCV 进程池必须有取消与 watchdog；超时/取消必须 `terminate()` 整个池。
- 字幕、manifest、检查点、结果快照、缓存 JSON 与工具 `dst` 必须临时文件 +
  原子替换；抽帧目录成功后再整体替换。
- `ArtifactCache` 哈希缓存与项目缓存注册表必须加锁，禁止并发重复哈希或多实例。
- 视觉证据缓存的键必须包含 source_hash、policy_version 与渲染常量；
  `render_hash` 不得包含 backend/driver/耗时，CPU/GL 只比较 `semantic_hash`。
- VLM/Planner 的 `evidence_refs` 只能引用当前 TileMap 中真实存在的 Txx 标签；
  未知标签必须拒绝，不得静默过滤或回退到自由描述。
- OpenGL 渲染器只做拼版/编号/bbox 叠层，禁止在 shader 中计算光流、帧差或
  直方图；每线程独立 context，失败必须回退 CPU 并记录 `backend=cpu`。
- M2 视图（帧差/光流条带/bbox/ROI/codecview）只作候选证据；codecview 不承诺
  数值级运动向量；ROI 必须回原素材取高分辨率，UI 证据叠层不得写 IR。
- 未保存时间线在切换项目或退出前必须确认；保存完成后再执行原动作。
- 窗口布局/最近项目等 UI 状态只能写 `desktop.json`；不得写进项目工作区。
- 桌面任务进度只用 Qt 信号；SSE 仅供可选服务端，WebSocket 未实现。
- 手动字幕必须写入 `EditIR.subtitles`（输出时间线）：`None`=自动、
  `[]`=明确无字幕、非空=手动覆盖；不得只存在 GUI 内存中。
- 全局音量/音调必须由 Compiler 转成单次 postprocess；音调必须保持时长，
  不得在 GUI/Agent 里直接拼 ffmpeg 滤镜。
- 预览音量/静音只影响监听并写 `desktop.json`，不得冒充成片音量。
- 多轨只能由 `EditIR.audio.tracks` 表达：None=旧单轨、[]=静音、非空=多轨；
  混音必须由 Compiler 生成一次 `mix_audio`，视频流保持 copy。
- 音频资产必须复制进项目 `audio/` 并写入 `project_audio_assets`；
  不得让 IR/GUI 引用工作区外路径。
- 分离器必须实现 `Separator` 协议，由 `build_separator()` 工厂选择；
  Demucs/UVR/MDX 只能通过外部进程适配器调用，禁止在桌面主进程 import torch，
  禁止把模型打进主 exe。
- 分离任务必须使用 Job 类型 `separate`，支持进度、取消、缓存与失败隔离。
- 分离结果必须按 source_hash+model 缓存并注册为音频资产；
  未配置 Demucs 时必须明确降级提示，不得静默失败。
- 循环音轨必须在输入层 `-stream_loop` 并由 Compiler 铺满剩余时长；
  不得在 GUI 中循环播放多个 player。
- 混音预览必须防抖、按轨道参数与资产哈希缓存到 `proxy/mix_*`；
  预览失败不得影响最终渲染。
- 音频 lane 波形必须按资产内容寻址缓存；不得按文件路径冒充缓存。
- 工具审计必须批量写（当前 20 条/批），任务结束/失败/取消时 flush。
- 时间线编辑只在 GUI 本地 segment 列表中进行；不得在 QWidget 内调用 Services。
- 所有编辑操作必须走 Undo/Redo 快照；保存为 revision 前不得写后端。
- 键盘快捷键必须避开文本输入/单元格编辑状态，且不得绕过编辑边界校验。
- 候选片段、revision diff、run metrics 只通过 Controller signal 更新 QWidget。
- 时间线缩放/平移必须使用可见区间换算，禁止整条时间线重绘不可见元素。
- 波形 peaks 必须走内容寻址缓存；缩略图必须异步解码并缓存 QPixmap，禁止 UI 线程批量读图。
- 字幕预览与烧录样式共用设置；颜色转换在 Compiler 完成，GUI 不拼 ffmpeg 样式。
- 章节摘要/转写上下文只读展示；字幕/波形/缩略图失败不得阻断编辑与渲染。

## 工作区铁律

1. 每个项目/任务使用独立工作区：`workspace/projects/{project_id}/`。
2. 用户源文件只读：`/ingest` 与 `/upload` 都必须复制进工作区后才能 probe/处理。
3. 所有工具路径相对于项目工作区；禁止直接操作源文件或工作区外路径。
4. 每次 Orchestrator 运行独占 `tmp/runs/{run_id}/`，保存 `result.json` 快照。
5. 沙箱必须拒绝绝对路径、盘符、`..`、NUL、符号链接逃逸与覆盖输入。

## 分层铁律

1. 前端不碰媒体文件，只处理 IR 与 URL；后端不生成 UI 逻辑。
2. 感知层只输出数值/时间戳，不写时间线、不调模型。
3. AI Planner 只输出 `EditIR`，禁止输出 ffmpeg 命令、路径拼接或 shell 片段。
4. Compiler 是纯函数：`EditIR + MediaFacts → ToolCall[]`，不做静默调整。
5. Runner 是唯一能起子进程的地方，且 `shell=False` + 白名单工具名。
6. 依赖方向：`contracts` 为叶；`perception/compiler` 只依赖 `contracts`；
   `planner` 可依赖 `contracts` 与 `llm`；`runner` 可复用 perception 的纯解析
   函数；`orchestrator` 只依赖 `contracts`、`llm` 与 `AgentToolkit` 协议；
   `server` 编排全部。基建（config/errors/logging/resources/cancel）可被各层
   使用。包间边以 `tests/test_import_boundaries.py` 的 `ALLOWED_EDGES` 为准，
   新增边必须登记并保证 QWidget 不 import server/runner/subprocess。
7. Orchestrator 是唯一构建/调度任务图的地方；子智能体不得自己起进程、
   不得直接操作媒体文件，只能通过 `AgentToolkit` 调用业务能力。
8. 子智能体必须无状态、线程安全；动态任务只能通过 `AgentResult.new_tasks`
   返回，任务 id 必须安全且依赖已存在。
9. 桌面 GUI 只能通过 `DesktopController` 调用 Services/Orchestrator；
   QWidget 不得直接调用 Services、不得起子进程、不得访问网络。
10. 工作线程（`Task` / `JobService`）只发 Qt Signal；禁止在工作线程操作 QWidget。
11. 桌面应用不启动 FastAPI/HTTP；单进程直连内核。
12. 应用数据写入 `%LOCALAPPDATA%\SmartCut`（或 `--data-dir`），禁止写 exe 目录。
13. DeepSeek Key 存 Windows Credential Manager；禁止把 Key 写入明文日志/配置。
14. 感知结果只写 `frames/` 下的 JSON/JPG；模型不得输出像素坐标或 crop 参数。
15. 9:16 crop 由 `compiler/fit.py` 根据归一化人脸锚点与源尺寸推导；
    anchor 只允许 `face_bbox` / `center`，无人脸时中心回退。
16. 字幕时间由 `compiler/captions.py` 按 trim 结构推导；模型不做输出时间算术。
17. 包装能力（fit/subtitles/loudnorm）必须走 Compiler + Runner 工具，禁止
    在 GUI/Agent 里直接拼 ffmpeg 命令。

## 禁止清单

- 任何 ffmpeg 命令字符串拼接；只能走 `runner/tools` 的 `build_argv`。
- `shell=True` / `os.system` / `eval` / 字符串形式命令行。
- 用浮点秒做时间运算；一律 `RationalTime(value, rate)`，仅在 argv 边界转十进制。
- 在 effects 里放变速、裁剪、转场等改变时长的操作；转场/变速必须使用
  `EditIR.transitions` / `TrimOp.speed` 与 `compose`/时长模型。
- 让模型输出文件路径、CLI 片段或 shell。
- 覆盖输入文件；输出必须走沙箱并显式 `-y`。
- 权限范围外的绝对路径、`..`、符号链接逃逸。
- 直接对用户上传的源文件执行 ffmpeg/分析；必须先复制进任务工作区。
- 子智能体在 `run()` 内调用 `subprocess` 或修改任务图以外的工作流。
- 在任务图中制造环、依赖不存在的节点，或让同一 agent 重复出现。
- 在 `desktop/widgets` 中直接调用 `Services`、`subprocess` 或 `httpx`。
- 在工作线程中更新 QWidget 或调用 `QMessageBox`。
- 在桌面包中硬编码 `ffmpeg`/`ffprobe` 路径或依赖系统 PATH。
- 在桌面包中启动 uvicorn/FastAPI 或写死 localhost 端口。
- 将 API Key 明文写入 `desktop.json`、日志或异常信息。
- 把任意媒体文件直接传给 faster-whisper；必须先抽成 16k mono WAV。
- 在桌面包中依赖系统 PATH 的 ffmpeg/ffprobe；必须使用内嵌资源。
- 让 LLM 输出 crop 坐标、字幕输出时间、文件路径或 ffmpeg 命令。
- 在 effects 中混入 speed/transition 等改变时长的操作。
- 在视觉感知失败时阻断规划/渲染；感知必须可降级并记录 warning。
- 用固定路径或“文件存在”代替内容寻址缓存；必须校验 source_hash 与参数。
- 绕开 `ResourceManager` 直接起 ffmpeg、OpenCV 扫描或 LLM 请求。
- 在长循环/子进程/转写块中忽略 `CancellationToken`。
- 通过修改旧 run 的 ledger 或源文件来实现“续跑”；必须新建 run 并复用缓存。
- 让 Critic 原地改写旧 Revision，或跳过 Compiler 直接执行 ffmpeg。
- 并行 cut 使用共享输出路径、忽略 seq 顺序，或把 `dst` 写进 segment 缓存键。
- 在 FTS 索引缺失/查询失败时直接报错；候选检索失败必须能降级到全量/顶层候选。
- 在 outline 阶段生成 candidates/plan/render，或在 Phase B 重新做章节选择。
- 让 LLM 或 VLM 输出图片路径、crop 坐标、章节边界或 ffmpeg 参数。
- 在未配置 VLM 时发送图片，或把 DeepSeek Key 发往非 DeepSeek 的 VLM 端点。
- 把硬件编码器探测放在模块导入/启动时，或失败后静默改用软件编码而不记录。
- 绕过 `src/smartcut/policies/*.json` 新增 agent/tool/encoder，或让旧数据库缺列继续运行。
- 在每次 Services 实例化/项目打开时重新计算整个源文件 SHA256。
- 为 fit+字幕+响度分别启动多次全片重编码。
- 在任务图里用轮次屏障阻塞已就绪任务。
- 在 Compiler/GUI 中直接删除音频或口癖；必须由 AudioAgent 提交新 revision。
- 让 QA Agent 修改 EditIR 或绕过 LLM 失败降级。
- 在未 clone / 未声明 thread_safe 时并行共享同一个转写模型。
- 在转写 worker 内并发写 SQLite 缓存；缓存写入必须回到主线程。
- 在生产路径的主进程 import faster-whisper 或加载模型；必须走独立 worker 进程。
- 后台任务完成/失败回调里弹模态 `QMessageBox.exec()`/`question()` 或 `QDialog.exec()`。
- 绕过项目生命周期锁提交 Job/创建 run，或在删除过程中让后台线程重建工作区。
- 让 `JobService` 队列无上限，或队列满时阻塞/无限等待。
- 直接写 manifest、字幕、检查点、结果快照或缓存 JSON；必须临时文件 + 原子替换。
- 让 OpenCV 进程池在没有取消/watchdog 的情况下无限等待卡死帧。
- 仅凭 `ffmpeg -encoders` 列表就把硬件编码器标记为可用。
- 视觉摘要命中整图缓存后仍打开/缩放图片，或图片未变时重新请求整批图片。
- 用单张 TileMap 的白名单校验多视图 `evidence_refs`，或让多个视图复用同一
  `T01`/`T02` 标签。
- 绕过 `vision.json` 的 `routing` 硬编码视图组合，或把 `default` 指向非
  `contact_sheet` 的高风险视图。
- 在感知线程中共享 Haar Cascade 对象，或绕开逐帧 `cpu_slot`。
- 在时间线拖拽/表格编辑时直接提交 revision 或覆盖源文件。
- 让全局快捷键在 QLineEdit/QTextEdit/单元格编辑中吞掉输入。
- 用候选片段或 diff 结果直接改写旧 revision；必须生成新 revision。

## 每个工具必须自带

- `--dry-run` 语义（编译层只生成 ToolCall，不执行）。
- 结构化结果（JSON）、超时、非零退出码即失败。
- 参数由 Pydantic 模型校验，工具名来自白名单注册表。

## 提交前

```bash
conda activate video_edit
python -m pytest -q
```
