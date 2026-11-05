# 性能与调度优化

本文记录 Phase 4 的长视频优化约束与实现位置。

## 1. source_hash 持久化

- `Services._register_asset` 在 ingest 时计算 SHA256 并写入
  `media_assets.source_hash`。
- `Services._source_hash` 优先读取 DB；旧库缺失时懒计算并回填。
- 目的：避免每次启动/打开项目对长视频重新全量哈希。
- 约束：源文件被替换必须走 ingest，重新计算并更新 DB。

## 2. 并行项目 bundle

- `DesktopController.open_project` 使用线程池并行加载
  project/revisions/runs/artifacts/perception/proxy/outline。
- 只有 `project` 和 `runs` 有依赖关系；perception/proxy 在确认有 asset 后并行。
- `list_orchestration_run_summaries` 不返回 plan/ledger/answer；
  双击运行记录时再调用完整 run 详情。
- SQLite 使用 WAL，读事务可并发；bundle 内均为只读查询。

## 3. 单次后处理

- `compiler/lower.py` 将 fit + 烧录字幕 + loudnorm 合并为一个
  `postprocess` ToolCall。
- 有视频 filter：一次 H.264 编码；只有 loudnorm：`-c:v copy`，仅音频编码。
- `fit/subtitles/loudnorm` 旧工具保留用于手工/兼容，但默认管线不再串联。
- 编码器 profile 仍由 Compiler 注入，Runner 不解析模型输出。

## 4. 依赖驱动调度

- `TaskGraphExecutor` 使用 ready queue + `wait(FIRST_COMPLETED)`。
- 任一节点完成立即提交新就绪节点，不再等待整批 future。
- 保留：并发上限、重试、失败跳过、动态任务、取消、检查点回调。
- `max_rounds` 仅作安全阀，默认 64。
- 约束：不能在 `_consume` 中阻塞等待其它任务。

## 5. 感知/视觉缓存

- `get_perception` 的内存缓存签名 = source_hash + frames/scenes/quality/faces/
  contactsheet/chapters 的 cache_key。
- 命中时直接返回同一 dict；调用方不得修改。
- `build_visual_notes` 先用 source_hash + 章节 id + 图片路径 + 模型 + prompt 版本
  查缓存；命中时不做 Pillow 读图/缩放。
- 视觉缓存仍以内容寻址为准；路径变化或模型变化会产生新键。

## 6. AudioAgent / SemanticQAAgent

- AudioAgent 在 plan 后运行，按词级时间戳删除配置的口癖词；
  提交新 revision，`audio.remove_filler` 置 false。
- SemanticQAAgent 在 Critic 后运行，输出 `EditReview`；
  LLM 失败/无 Key 时用规则审片。QA 不修改时间线，只影响警告与最终置信度。

## 7. 分块转写并行与静音筛选

- 静音块预筛选：`chunking.wav_rms` 读取 16k mono PCM，归一化 RMS 低于
  `SMARTCUT_TRANSCRIBE_SILENCE_RMS`（默认 0.002）且块长 ≥0.5s 时跳过模型，
  写入空 Transcript 缓存。
- 并行：`SMARTCUT_TRANSCRIBE_WORKERS`（0=auto）。
  auto 仅在 CPU + transcriber 提供 `clone()` 时使用 2 个 worker；
  GPU 默认 1，避免显存翻倍。
- 生产路径 `build_transcriber()` 返回 `ProcessWhisperTranscriber`：模型只在
  spawn 的独立 worker 进程内加载与推理；取消/超时直接 kill 该进程，主进程
  不 import faster-whisper。`SMARTCUT_WHISPER_WORKER_TIMEOUT_SECONDS`
  控制单次请求上限（含模型加载）。
- worker 崩溃/管道断开或单次超时后按 `SMARTCUT_WHISPER_WORKER_RETRIES`
  （默认 1）重启进程并重试；用户取消不重试，重试耗尽后抛出原始错误。
- `ProcessWhisperTranscriber.clone()` 为每个 Job/worker 创建独立进程；
  `FasterWhisperTranscriber` 仅保留给 self-check/基准直连使用。
- 每个 worker 仍通过 `cpu_slot`；缓存读写留在主线程，避免 SQLite 并发写。
- faster-whisper 内部 `vad_filter=True` 保留；静音块筛选是额外的廉价预过滤。
- 结果新增 `cache_misses`、`silent_chunks`、`transcribe_workers`。

## 8. P1 预览/感知优化

### 视觉摘要双层缓存

- 第一层：source_hash + 章节 id + keyframe/contact sheet 路径 + model +
  prompt_version + goal；命中时不读图。
- 第二层：单图 sha256 + model/base_url/prompt_version + 章节 + goal；
  只对缺失图片发起一次批量 VLM 请求，响应按 image_index 写回单图缓存。
- 整图缓存被删除但单图缓存仍在时，不再调用 VLM；部分图片变化只重算缺失图。
- 最终 `VisualNotes` 由单图描述按章节在本地归并。

### 编码器试编码

- `ffmpeg -encoders` 只说明构建包含编码器；`EncoderSelector` 还会对每个硬件
  编码器执行一次 64x64/1 帧 `-f null -` 试编码。
- 失败编码器从可用集合剔除，结果按 ffmpeg 路径+size/mtime 缓存。
- 避免第一次正式渲染才失败并触发回退。

### 感知并行与自适应采样

- `analyze_quality` 将逐帧静态度量（mean/std/Laplacian/dhash/缩小灰度）
  放入线程池；冻结/重复判断仍按时间顺序串行，使用 160x90 灰度降低内存。
- `detect_faces` 按帧并行；Haar Cascade 使用 thread-local 实例，避免共享
  OpenCV 对象。
- 每帧任务通过 `ResourceManager.cpu_slot`，总并发受 `SMARTCUT_CPU_CONCURRENCY`。
- `SMARTCUT_PERCEPTION_MAX_FRAMES`（默认 1200，0=不限）限制质量/人脸分析帧数；
  有 scenes 缓存时优先保留镜头切点附近帧，其余均匀采样。
- 任务图调整为 `frames → scenes → {quality ∥ faces ∥ contactsheet}`，
  quality/faces 依赖 scenes 以使用场景自适应采样。
- 质量/人脸缓存键包含 scenes cache_key 与 max_frames，设置变化会自动失效。

## 9. 可观测性与审计

### Orchestrator 指标

每次 run 的 `metrics` 持久化到 `orchestration_runs.metrics_json`，包含：

- `tasks[]`：task_id、agent、status、attempts、retries、queue_wait_ms、
  duration_ms、cache_hits/cache_misses、llm_requests、prompt/completion/total tokens、
  encoder。
- `by_agent_duration_ms`：按 agent 聚合的耗时。
- `totals`：队列等待、总耗时、重试次数、缓存命中/未命中与命中率、
  LLM 请求/缓存命中、token 总数。
- `encoders`：本次实际使用的编码器集合。
- `wall_duration_ms` / started_at / finished_at。

LLM 客户端在响应 `usage` 中记录 token；缓存命中单独计数。
PlanAgent 与 QA Agent 的 usage 进入账本，最终汇总到 metrics。

### 候选 FTS top

- `top()` 使用 `json_extract(data_json, '$.score')` 在 SQLite 内排序并限制条数，
  不再加载最多 500 条完整 JSON 到 Python。
- 无 JSON1 时回退内存排序。
- top 结果按 `(project, source, limit)` 进程内缓存；重建/清空候选项时失效。

### 工具审计批量写

- `JobReporter` 缓冲工具调用，每 20 条批量写 `tool_call_records`。
- 任务结束/失败/取消时 `flush_tools()` 写入剩余记录。
- 降低长渲染的逐条 SQLite 事务与 WAL 写放大。

## 10. P2 并行与 GPU

### 公平资源池

- `ResourceManager` 使用 `FairLimiter` 替代裸 Semaphore：
  - 按 `(priority, sequence)` 排队，避免长任务持续抢占；
  - 支持 `timeout` 与 `deadline`，超时抛 `ResourceTimeout`；
  - `cpu_slots(n)` 供进程池一次持有多个 CPU 配额；
  - `snapshot()` 暴露 active/waiting/timeouts/max_wait_ms。
- `SMARTCUT_RESOURCE_WAIT_TIMEOUT_SECONDS`（默认 0=自动，至少覆盖最长
  工具超时，默认 1800s）；长流水线可按需设置，仍在阶段 timeout 约束内。
  资源等待支持取消令牌，取消最多延迟一个轮询周期（0.2s）。

### OpenCV 进程池

- `perception/process_pool.py` 提供质量/人脸逐帧 worker（Windows spawn）。
- `SMARTCUT_PERCEPTION_PROCESS_POOL=auto|on|off`；auto 仅在帧数达到
  `SMARTCUT_PERCEPTION_PROCESS_POOL_MIN_FRAMES`（默认 240）且非 frozen
  桌面包时启用。
- 父进程通过 `cpu_slots(workers)` 持配额；worker 内不写 SQLite、不共享
  OpenCV 对象。进程池失败自动回退线程池，`QualityReport.engine` /
  `FaceReport.engine` 记录 `process` 或 `thread_fallback`。
- 有取消令牌或 watchdog 时使用可中断 `multiprocessing.Pool`：逐帧取结果并
  轮询取消；取消或 `SMARTCUT_PERCEPTION_PROCESS_POOL_TIMEOUT_SECONDS`
  （默认 600s）超时执行 `terminate()` 回收整个池，不再等待 `map` 返回。
- watchdog 超时抛 `ToolExecutionError`，不再回退到可能同样卡死的线程池。

### GPU 解码

- `HardwareDecodeSelector` 通过 Runner 执行
  `ffmpeg -hwaccel <backend> -frames:v 1 -f null -` 探针，按 ffmpeg
  二进制签名缓存结果。
- `SMARTCUT_HARDWARE_DECODING=auto|off`、`SMARTCUT_DECODE_HWACCEL=cuda`。
- 抽帧与 proxy 均记录实际 `decoder`；运行时失败立即禁用后端、清理
  半成品并回退 CPU，warning 进入 Agent 账本。
- 抽帧缓存键包含 decoder，CPU 回退结果可在后续运行复用。

### 转写 GPU

- `SMARTCUT_WHISPER_DEVICE=auto` 时在后台转写线程中通过
  `ctranslate2.get_cuda_device_count()` 懒检测，不 import torch。
- CUDA 可用时强制单 worker，避免多进程共享 CTranslate2 模型；
  CPU + clone 仍可自动使用 2 worker。
- `SMARTCUT_WHISPER_GPU_COMPUTE_TYPE`（默认 float16）仅用于
  `compute_type=auto`。
- 转写结果包含 `transcribe_device`。

### 性能基准

```bash
python scripts/benchmark_long_video.py --src D:\video.mp4 --repeat 2
python scripts/benchmark_long_video.py --src D:\video.mp4 --process-pool on --transcribe
```

基准输出每阶段耗时、缓存命中、分析 engine、转写 worker/设备。

## 11. 视觉证据层（M0–M2）

### 指标

`build_visual_evidence` 返回并写入 `evidence.json`/缓存 meta：

- `extract_ms / measure_ms / render_ms / duration_ms`
- `tiles / windows / estimated_tiles / estimated_tokens / dropped_windows`
- `image_bytes / backend / renderer_fallback / measure_backend / cache_hits`
- `kind`（contact_sheet / temporal_diff / flow_strip / bbox_overlay /
  roi_crop / codec_residual）

`render_hash` 用于同机复现，排除 backend/driver/耗时与 `measure_backend`；
CPU/GL 跨后端只比较 `semantic_hash`（≤6/64，通道均值差 ≤3/255）。

### 缓存与预算

- 缓存键：`source_hash + kind + tile_index + roi_target + label_start + dedupe +
  backend + fps + policy_version + frames_key + transcript_sig + selection +
  指纹版本/engine + 渲染常量`；
- 目录：`cache/evidence/<key>/view.png + evidence.json`，临时目录原子替换；
- top-K：镜头/质量/章节/转写/人脸种子 → 合并窗口 → 价格与 token 硬预算；
- 设备分档：CPU 最多 5 图、tile ≤320×180；GL 使用配置上限；
- 可重算缓存 prune 白名单包含 `evidence`，不淘汰 segments/renders/manifest/result。

### GL/CPU 基准

```bash
python scripts/benchmark_evidence_render.py --tiles 9 --repeat 3 \
  --kind temporal_diff --assert-not-slower
```

- GL 用每线程 `QOffscreenSurface/QOpenGLContext/FBO + QOpenGLPaintDevice`；
- 绘制采用 source-rect 由 GPU 缩放，避免 CPU `QImage.scaled`；
- 上下文/驱动失败自动回退 Pillow，记录 `backend=cpu`；
- 本机 Windows 参考：temporal_diff CPU ≈96ms / GL ≈70ms（随素材与驱动变化）。

### 批量渲染与 GpuMeasurer（P2）

- `render_many(measure, requests)`：同一 measure 一次 context、共享解码图片缓存，
  循环输出多个网格/kind；`synthesize_many` 逐请求生成独立 evidence/hash。
  GL 仍只做拼版/编号/bbox，不在 shader 计算光流/帧差/直方图。
- `perception/view/measure_gpu.py` 的 GpuMeasurer 与 renderer 解耦：
  - CPU 参考路径 `measure_tiles`；
  - CPU/GPU 用 `compare_measure_results` 做数值容差比对；
  - GPU 失败回退 CPU，记录 `measure_backend`（不参与 render_hash）；
  - `SMARTCUT_GPU_MEASURE_ENABLED` 且后端非 CPU 时，候选窗口采样可提升到
    `SMARTCUT_GPU_MEASURE_FPS`（≤30fps），CPU 保持 `view_fps`。
- 当前 OpenCV Python CUDA 绑定有限，`CudaViewMeasurer` 先做能力探测并使用
  CPU 数值参考；未来替换 kernel 不改变 `MeasureResult` 契约。

## 12. 任务队列、项目生命周期与原子写

### 任务队列与生命周期

- `JobService` 活动/排队 Job 上限由 `SMARTCUT_MAX_PENDING_JOBS`（默认 64，
  0=不限制）控制；满时快速失败（API 422），不无限堆积。
- `delete_project` 持有项目生命周期锁：删除期间新的 `jobs.submit` /
  `create_orchestration_run` 立即失败；删除成功后项目进入永久 tombstone，
  等待锁的提交不会在删除后落地。
- `JobService.cancel` 对排队任务即使状态写库失败，也保证执行 `on_cancel`
  hook 并清理 token/future，避免 OrchestrationRun 永久停在 queued/running。

### 原子落盘

- `server/atomic.py::atomic_write_text`：同目录临时文件 + `os.replace`。
- 覆盖字幕 SRT/VTT、`frames/manifest.json`、checkpoint context digest、
  `result.json`、缓存 JSON/TXT。
- `ToolCallExecutor` 的 `dst` 产物同样先写临时文件，成功后原子替换；取消/
  失败清理临时文件。
- 抽帧先写 `cache/frames/{key}.part-*`，成功后整目录替换；取消/失败删除
  半成品目录。
- `ArtifactCache` 的哈希缓存与 `_project_caches` 均有锁，避免并发重复哈希
  或创建多个 cache 实例。

## 13. 验证命令

```bash
conda activate video_edit
python -m pytest -q
python -m pytest -q tests/test_resources.py tests/test_process_pool.py \
  tests/test_gpu_services.py tests/test_benchmark.py
python scripts/check_drift.py
python -m pytest -q -m eval
python scripts/benchmark_evidence_render.py --tiles 9 --repeat 2 \
  --kind temporal_diff --assert-not-slower
```
