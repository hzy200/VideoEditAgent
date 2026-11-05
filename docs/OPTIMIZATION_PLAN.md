# SmartCut 工作流与性能优化计划

- 状态：P0/P1/P2/P3 已完成（2026-11-02）；后续视觉证据层见 `docs/VISION.md`，
  最新测试基线以 README 为准（563 collected / 554 passed + 9 skipped）
- 日期：2026-11-02
- 范围：视频理解、抽帧与 VLM、结构化元数据、并行/GPU、测试与回归
- 关联文档：`docs/PERFORMANCE.md`、`docs/VISION.md`、`docs/MEDIA_PIPELINE.md`

## 1. 背景

当前系统已经具备较完整的桌面剪辑与多智能体流水线：

- 依赖驱动的 Orchestrator 任务图；
- 内容寻址缓存（源哈希 + 参数）；
- 感知：镜头/黑帧/质量/人脸/contact sheet；
- 分块转写与章节化；
- 候选片段 + FTS5；
- Planner / Critic / AudioAgent / SemanticQAAgent；
- 两阶段大纲、硬件编码、Proxy-first；
- 多轨音频与 Demucs 伴生 worker（CUDA 已验证）；
- 本轮历史基线：232 项全量测试（230 passed + 2 skipped），live 2 passed；
  当前全量测试见 README（563 collected / 554 passed + 9 skipped）。

但长视频、复杂画面和多模态调用仍存在成本和耗时压力，主要来自：

1. 全片高帧率解码、重复读图与多次缩放；
2. 质量/人脸/contact sheet 等阶段各自读取帧；
3. VLM 输入图片数量与 token 成本；
4. 章节/候选/转写/视觉结果缺少统一结构化元数据；
5. 长视频缺少“只处理选中章节”的增量工作流；
6. CPU 逐帧分析与 ffmpeg/LLM 并发边界仍可细化；
7. GPU 目前主要用于 Demucs/编码，视频解码与部分分析尚未充分利用。

---

## 2. 目标

在不明显降低剪辑效果的前提下：

1. 减少无效抽帧与 VLM 图片数量；
2. 把视频理解结果变成可缓存、可增量更新的结构化元数据；
3. 让长视频只计算用户关心的章节；
4. 降低重复解码、重复读图与重复模型调用；
5. 保持所有优化可降级、可取消、可审计；
6. 用测试锁定行为，避免“优化后效果变差”。

---

## 3. 测试基线与回归

### 3.1 当前基线

- 本轮历史默认测试：**232 collected，230 passed + 2 skipped（live）**；
  当前基线：**563 collected，554 passed + 9 skipped**
- live DeepSeek：**2 passed**
- `scripts/check_drift.py`：通过
- 开发态 `--smoke-test`：退出 0

本轮新增 53 个测试，覆盖：

- 契约：音轨/字幕/编码器/Schema；
- 编译器：单次 postprocess、visual→mix、amix/loop/adelay；
- Runner：校验、沙箱、取消、抽音频参数；
- 缓存：键稳定性、JSON 往返、文件失效、源哈希；
- 服务：音频资产、波形、预览混音、诊断、revision 往返；
- API：404/422/诊断/任务过滤/outline；
- GUI：主题、配置损坏、字幕位置、时间线缩放/撤销、stem 角色、空状态。

### 3.2 优化阶段必须新增的回归

每个优化项都必须有对应测试：

- 抽帧合并后帧清单/时间戳/数量一致；
- contact sheet 尺寸、标签与缓存键一致；
- `VideoManifest` 序列化/反序列化/增量失效；
- 自适应抽帧在预算内且优先保留场景/风险帧；
- VLM 图片选择上限、单图缓存、text-only 降级；
- 快速大纲不调用 VLM/质量/人脸；
- 章节增量工作流只处理选中区间；
- GPU 解码失败自动回退 CPU；
- 并行/进程池不绕过 `ResourceManager`；
- 缓存命中后不重新解码/不重新请求 VLM。

---

## 4. 视频理解与工作流优化

### 4.1 统一 `VideoManifest`

新增结构化元数据工件，组合现有契约，而不是复制数据：

```text
VideoManifest
  source_hash
  duration
  shots: ShotBoundary[] + keyframe refs + scene scores
  chapters: ChapterPlan + chapter summaries/keyframes
  transcript: segments/words digest + chunk cache keys
  quality: QualityReport
  faces: FaceReport
  candidates: counts/top tags
  visual_notes: ChapterVisualNote[]
  cache_keys: stage -> key
  version / created_at
```

建议文件：

- `contracts/manifest.py`：`VideoManifest` + 版本字段；
- `perception/manifest.py`：组装、校验、增量合并；
- `server/services/facade.py`：`build_video_manifest(project_id)` 与缓存；
- 落盘为 `perception/manifest.json` 或 artifact 表记录。

设计原则：

- 只引用帧/关键帧/字幕的路径或 hash，不重复大块内容；
- 每个字段可追溯到阶段缓存键；
- `source_hash` 变化才整体失效；
- 章节变化只重算受影响章节的摘要与关键帧；
- Plan/VLM 只读 manifest，避免各自拼 JSON。

### 4.2 自适应抽帧

当前已有帧清单、场景切点、质量/人脸帧上限和 contact sheet。
下一步从“固定上限”转为“预算 + 优先级”：

优先级从高到低：

1. 硬切点前后帧；
2. 质量风险帧（黑帧、模糊、重复帧）；
3. 转写话题变化点；
4. 章节首尾帧；
5. 均匀覆盖帧（补足预算）。

预算维度：

- 最大帧数；
- 最大 VLM 图片数；
- 最大图片字节/token；
- 长视频按章节分配；
- 快速模式只保留 contact sheet。

去重：

- perceptual hash / 颜色直方图；
- 相邻帧相似度阈值；
- 同一章节最多 1–2 张关键帧。

### 4.3 VLM 输入优化

现有：

- 全局 contact sheet + 每章最多 2 张关键帧；
- 图片长边限制、JPEG 压缩、单图/整图双层缓存；
- 最多 7 张图、失败 text-only。

建议：

1. **文本优先**：先看转写/章节摘要/候选标签；
   仅对缺失转写、视觉差异大、目标依赖画面或 Planner 低置信度章节调用 VLM。
2. **图片合并**：同章节多帧拼 2×2 tile，减少图片数量与 token。
3. **单图补算**：只请求 sha256 变化的图片，不重复整批。
4. **单飞**：同一图片 + prompt 并发请求只保留一个。
5. **低置信度二次请求**：仅对 `confidence` 低或 `cautions` 多的章节补图。
6. **缓存键**：图片 sha256 + prompt_version + model + base_url，禁止按路径。
7. **降级**：超时/格式错误/额度不足直接 text-only，不阻断规划。

### 4.4 新工作流

#### A. 快速大纲（Fast Outline）

```text
probe → scenes → 少量帧 → contact sheet → chapters
```

跳过：

- 质量/人脸全量分析；
- 完整转写；
- VLM；
- 候选/Plan/Render。

用途：长视频先确认章节结构，再进入精细规划。

#### B. 按章节增量编辑

```text
用户勾选章节
→ 只抽该章节帧/转写片段
→ 章节级质量/人脸
→ 章节级 VLM（可选）
→ 候选检索
→ Plan/Critic/Render
```

要求：

- 章节外缓存不失效；
- 任务图只展开选中章节；
- 渲染只处理相关 Segment；
- 失败/取消不影响其他章节。

#### C. 音频优先（Audio-first）

```text
Demucs/转写 → 静音/话题边界 → 抽帧 → 视觉 notes → Plan
```

适合访谈、课程、口播。
语音/话题边界可以显著减少需要看画面的帧数。

#### D. 审片工作流

对渲染结果做：

- 画面/字幕/音频 QA；
- 关键帧 VLM 复核；
- 输出问题列表，不直接改 IR。

---

## 5. 性能优化

### 5.1 先量化

在现有 metrics 上增加：

- 每阶段 wall/CPU/GPU 时间；
- 解码帧数、丢弃帧数；
- VLM 图片数、图片字节、token；
- 缓存命中/未命中与单阶段耗时占比；
- 长视频分章节耗时。

没有数据就不要盲目加大线程池。

### 5.2 减少重复解码

优先级最高：

1. **一次 ffmpeg 完成抽帧 + 缩略图 + contact sheet 素材**
   - `fps`/`select` + `scale` + `tile`；
   - 避免多次全片解码。
2. 抽帧时直接缩放到目标尺寸；
   - 后续不再重复读大图/缩放。
3. 帧/缩略图/contact sheet 全部按源哈希和参数缓存；
4. 未选章节不抽帧、不转写、不调用 VLM。

### 5.3 并行策略

- 线程：ffmpeg 子进程、LLM 请求、SQLite 读；
- 进程池：OpenCV/Pillow 逐帧 CPU 密集分析，绕过 GIL；
  - 仅后台服务/worker 使用；
  - 不传 QWidget；Windows 使用 spawn；
- 所有并发继续经过 `ResourceManager`；
- 避免线程池叠加：Job 池、Orchestrator 池、帧池共享配额；
- 增加阶段级 deadline 与公平性，避免长任务饿死其他任务。

### 5.4 GPU 策略

- Demucs：已使用 CUDA，已验证 GPU 利用率；
- 编码：NVENC/QSV/AMF 已有，失败回退 libx264；
- 解码：评估 `-hwaccel cuda` / `h264_cuvid` 用于抽帧/代理；
  - 必须保留 CPU 回退；
  - 校验颜色/分辨率/时间戳一致。
- 转写：faster-whisper 在 CUDA 可用时可走 GPU；
  - 不要多 worker 共享同一 CTranslate2 模型。
- OpenCV CUDA：收益不稳定、打包困难，优先级低；
  - 先用进程池；
  - **不在 GL shader 内计算光流/帧差/直方图**（会破坏 render_hash 与 CPU/GL
    语义等价）；
  - 已提供独立 `GpuMeasurer`：只输出数值 `MeasureResult`，CPU 等价/容差/回退，
    GPU 可用时候选窗口采样提升到 10–30fps；kernel 可替换不影响契约。
- 批量视图合成：`render_many` 一次 context + 解码图片缓存，循环输出多个
  网格/kind；这是 GL 提升 LLM 信息密度的主要用法。
- VLM：远程服务，优化重点是减少图片/token 与并发批处理，不是本机 GPU。

### 5.5 缓存与单飞

- 阶段结果缓存：probe/frames/scenes/quality/faces/contact sheet/transcript/chapters/VLM/plan/render；
- 源哈希变化才全量失效；
- 参数变化只失效对应阶段；
- 同一任务并发请求同一缓存键时使用 single-flight；
- VLM 单图缓存 + 整批缓存；
- Segment 渲染缓存继续按去 dst 参数键；
- 预览混音防抖 + 参数哈希缓存。

### 5.6 渲染与预览

- 已有：单次 postprocess、Segment 缓存、并行 cut、硬件编码；
- 继续避免：
  - fit/字幕/响度多次全片重编码；
  - 为预览生成高质量成片；
  - 每次修改都重新渲染全部 Segment；
- 代理优先、预览混音缓存、按需切换成片。

---

## 6. 实施阶段

### P0：低风险高收益（已完成）

1. ✅ 合并抽帧/缩略图/contact sheet 素材为单次 ffmpeg（`split` 双路输出）；
2. ✅ 自适应帧预算 + 时间间隔/感知哈希去重；
3. ✅ VLM 单图补算 + 单飞 + 文本优先；
4. ✅ 阶段耗时与 VLM 图片/字节/token 指标；
5. ✅ 回归测试与缓存键校验（`tests/test_frames_manifest.py`、
   `tests/test_adaptive_sampling.py`、`tests/test_vision_budget.py`、
   `tests/test_stage_metrics.py`）。

### P1：结构化与增量（已完成）

1. ✅ `VideoManifest` 契约、增量合并与内容寻址缓存；
   `perception/manifest.py`、`GET /api/projects/{id}/manifest`；
2. ✅ 章节级增量分析：approved outline 的 `chapter_ids` 进入
   `AgentContext`，质量/人脸使用独立 `quality_chapter`/`faces_chapter`
   缓存，规划/候选/VLM 只处理选中章节；
3. ✅ 快速大纲：`phase=fast_outline`，只执行
   analyze → frames → scenes → contactsheet → chapters；
4. ✅ VLM 同章节 2x2 tile + 摘要为空/cautions 过多的二次复核；
5. ✅ API/GUI：manifest 查询、快速大纲选项、感知面板结构化摘要。

### P2：并行与 GPU（已完成）

1. ✅ OpenCV 质量/人脸分析可选 spawn 进程池，父进程持有 CPU 配额，
   失败回退线程池；
2. ✅ 抽帧与 proxy 支持 hwaccel，Runner 探针 + 运行时 CPU 回退；
3. ✅ whisper device=auto 懒检测 CUDA，GPU 强制单 worker；
4. ✅ 公平资源池：优先级队列、timeout/deadline、等待指标；
5. ✅ `scripts/benchmark_long_video.py` 与可复用 benchmark runner。

### P3：新工作流（已完成）

1. ✅ 音频优先：`phase=audio_first`，transcribe 先于 frames，
   后续章节/候选/规划共享转写与话题边界；
2. ✅ 渲染审片：`review_render` + `RenderReview`，检查时长/音轨/比例/
   字幕/黑帧模糊，可选 VLM 关键帧复核，只报问题不改 IR；
3. ✅ 模板/批量：`EditTemplate` 文件存储、apply-template 与批量编排 Job；
4. ✅ 可选本地视觉：`vision_provider=local` 插件适配器，支持 ONNX 后端
   可用性检查，缺失时自动 text-only。

### P4：语义层与 GL 定位（已完成）

1. ✅ 证据路由与多视图：`vision.json routing`、标签不相交、一次抽帧、
   ROI/二次复核按 measurements.score 选异常时刻；
2. ✅ 测量事实与 `visual_facts` 注入 Planner；
3. ✅ 屏幕文字 OCR：cv2 候选区 + 独立 worker + 逐帧内容缓存 + 章节/Planner；
4. ✅ centroid+IoU 人脸跟踪/EMA 平滑，改善 9:16 裁切；
5. ✅ RANSAC 仿射拆分相机/主体运动，识别 pan/tilt/zoom/B-roll；
6. ✅ HSV 镜头聚类，章节只在视觉簇边界切分；
7. ✅ 统一 fingerprint（dhash:v2，cv2 优先/Pillow fallback）；
8. ✅ `render_many/synthesize_many` 批量视图合成；
9. ✅ 独立 GpuMeasurer：CPU 等价/容差/回退与 10–30fps 采样入口；
10. ✅ QOpenGLWidget 预览/叠层仅改善人审，明确不进入本轮实现。

---

## 7. 验收标准

- 相同源文件 + 相同参数，优化前后帧清单/字幕/计划结构一致或可解释；
- 快速大纲不触发 VLM、人脸/质量全量分析；
- 章节增量只处理选中章节，其他缓存不失效；
- VLM 图片数/token 明显低于当前基线；
- VLM 失败自动 text-only，不阻断渲染；
- GPU 解码/编码失败自动回退 CPU；
- 所有并发通过 `ResourceManager`，取消可生效；
- 缓存命中不重复解码/不重复请求模型；
- 全量测试、live、drift、smoke 继续通过；
- 长视频基准记录每阶段耗时与缓存命中率。

---

## 8. 风险与回退

| 风险 | 缓解 |
|---|---|
| 抽帧合并导致时间戳/帧数变化 | 保留旧路径开关；golden 帧清单测试 |
| 自适应抽帧漏掉关键画面 | 场景切点/风险帧优先；可切换精细模式 |
| VLM 图片减少导致描述变差 | 文本优先 + 低置信度二次请求 |
| VideoManifest 缓存失效不精确 | 每字段记录来源 cache key；增量测试 |
| 进程池在 Windows 启动开销 | 仅长任务启用；批量提交；测量阈值 |
| GPU 解码颜色/裁切差异 | CPU 回退 + 输出对比测试 |
| GPU 包体过大 | 已采用伴生 worker；可按需 CPU-only 构建 |
| 并发池叠加过载 | 统一 ResourceManager + 阶段 deadline |

---

## 9. 明确不在本计划

- 完整 NLE 多轨时间线/转场/变速；
- OTIO/FCPXML 交换；
- 实时 VLM 视频流理解；
- 多机分布式渲染；
- 用 VLM 直接生成 EditIR；
- 绕过 Compiler 直接拼 ffmpeg。

---

## 10. 关键文件

```text
src/smartcut/contracts/manifest.py          # 新
src/smartcut/perception/frames.py
src/smartcut/perception/contactsheet.py
src/smartcut/perception/manifest.py         # 新
src/smartcut/planner/vision.py
src/smartcut/orchestrator/agents/*.py
src/smartcut/orchestrator/planner.py
src/smartcut/server/services/
src/smartcut/runner/tools/frames.py
src/smartcut/resources.py
src/smartcut/policies/workflow.json
src/smartcut/desktop/widgets/perception_panel.py
tests/test_frames_manifest.py               # 新
tests/test_video_manifest.py                # 新
tests/test_adaptive_sampling.py             # 新
tests/test_vision_budget.py                 # 新
tests/test_incremental_chapters.py          # 新
```

## 11. 验证命令

```bash
conda activate video_edit
python -m pytest -q
python -m pytest -q tests/test_video_manifest.py tests/test_adaptive_sampling.py tests/test_vision_budget.py
python scripts/check_drift.py
SMARTCUT_LIVE_DEEPSEEK=1 python -m pytest -q -m live
python -m smartcut.desktop.app --smoke-test --data-dir %TEMP%/smartcut-smoke
```
