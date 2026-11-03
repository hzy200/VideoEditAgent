# SmartCut — 智能剪辑桌面客户端

单进程 Windows 桌面应用：AI 写时间线、Orchestrator 调度子智能体、
ffmpeg 编译时间线、用户只审时间线。

**除可选的 DeepSeek/VLM 服务外，其余全部本地完成**：ffmpeg/ffprobe、
faster-whisper 本地模型、SQLite、项目工作区、Orchestrator。也可用本地视觉
插件替代远程 VLM。可打包为 `SmartCut.exe`。

## 功能

- **纯客户端 GUI（PySide6）**：不启动 FastAPI、不监听端口；直接调用进程内 Services。
- **Orchestrator 多智能体**：默认由 `workflow.json` 规则图驱动，LLM 只负责章节选择、
  规划、审片与可选总结；支持并行子智能体、动态任务、失败重试/跳过、最终决策答案。
  如需让 LLM 生成任务图，可在设置中显式开启实验选项。
- **每任务独立工作区**：文件复制到 `workspace/projects/{id}/`，源文件只读。
- **本地媒体能力**：ffprobe、cut/concat、代理预览、转写（faster-whisper）。
- **OpenCV 感知**：PySceneDetect 镜头切分、黑/静/糊/重复帧、人脸锚点、contact sheet。
- **候选片段 + FTS5**：转写句群融合镜头/质量/人脸，按目标做中英文检索。
- **Critic 局部重规划**：移除黑帧/模糊/重复帧区间，按镜头拆分过长片段，自动提交新 revision。
- **包装**：9:16 重构图（人脸锚点+中心回退）、SRT/VTT、可选烧录、-14 LUFS 响度归一。
- **长视频流水线**：分块转写（静音块预筛选 + 可选并行 worker）、
  章节切分/ChapterPlan、内容寻址缓存、增量 ledger 检查点与崩溃恢复。
- **可控执行**：取消 token、Job 取消、缓存辅助续跑、FFmpeg/CPU/LLM 并发上限。
- **渲染优化**：Segment 级缓存、并行 cut、revision 结构 diff。
- **两阶段大纲**：长视频先出章节大纲，人工勾选/改名/排序后再继续规划与渲染。
- **快速大纲 / 音频优先 / 章节增量**：
  - `fast_outline` 只做抽帧/镜头/contact sheet → 章节，跳过质量/人脸/完整转写；
  - `audio_first` 先转写/话题边界再抽帧，适合访谈、课程、口播；
  - Phase B 把确认章节的 `chapter_ids` 贯穿感知、候选、VLM 与规划，
    章节外缓存不失效。
- **VideoManifest**：统一结构化视频理解清单（镜头/章节/转写摘要/质量/人脸/
  候选/VLM notes/阶段缓存键），按源哈希与章节增量缓存，Planner 只读压缩摘要。
- **自适应感知**：一次 ffmpeg 同时输出分析帧与 contact-sheet 缩略图；
  按镜头切点/质量风险/话题变化/章节边界分配帧预算，并用感知哈希去重。
- **VLM 成本优化**：文本优先、章节 2x2 tile、单图 sha256 补算、single-flight、
  低置信度章节二次复核；未配置/失败自动 text-only。
- **硬件加速**：硬件编码 NVENC/QSV/AMF；GPU 解码（hwaccel）失败自动回退 CPU；
  OpenCV 长任务可选 spawn 进程池；whisper `device=auto` 懒检测 CUDA，
  GPU 强制单 worker。
- **渲染审片**：对成片检查时长/音轨/比例/字幕/黑帧模糊，可选 VLM 关键帧复核，
  只输出问题与评分，不修改 EditIR。
- **模板与批量**：编辑模板（目标、phase、9:16/字幕/响度/去口癖）可复用；
  批量编排多个项目并汇总成功/失败与产物。
- **Proxy-first 预览**：打开项目先播放 720p 代理，成片完成后再切换。
- **多模态视觉摘要**：contact sheet + 章节 keyframes/tile 送入可配置 OpenAI
  兼容 VLM，生成视觉 notes 注入文本 Planner；未配置或失败时自动降级。
- **可选本地视觉**：`vision_provider=local` 通过外部插件模块调用本地 VLM
  或 ONNX 推理，主程序不打包/加载 torch；依赖或模型缺失时自动 text-only。
- **AudioAgent**：按词级时间戳去口癖并提交修正 revision。
- **SemanticQAAgent**：LLM/规则审片，输出问题、score 与警告，不直接改时间线。
- **长视频性能**：source_hash 持久化、项目 bundle 并行加载、依赖驱动调度、
  视觉摘要双层缓存、编码器试编码、感知按帧并行与场景自适应采样、
  fit+字幕+响度单次编码；公平资源池（优先级/timeout/deadline）、
  可选 OpenCV 进程池、GPU 解码与转写策略。
- **可观测性**：每次 run 记录按 agent 的队列等待/耗时/重试、缓存命中率、
  LLM token、编码器、VLM 图片/字节/token、解码器、分析 engine、
  抽帧/分析/丢弃帧数与阶段耗时；FTS top 内存排序 + 缓存；
  工具审计每 20 条批量写。
- **性能基准**：`scripts/benchmark_long_video.py` 按阶段输出耗时、缓存命中、
  分析 engine、转写 worker/设备，便于长视频回归对比。
- **GUI 直接编辑**：时间线拖拽移动/边缘裁剪、添加/删除/分割/排序、撤销/重做；
  选中片段循环预览；空格/左右逐帧/Delete/Ctrl+Z 快捷键。
- **播放与监听**：QtMultimedia 视频/音频预览，播放/暂停/停止、音量滑杆、
  静音、逐帧与循环；预览音量随窗口状态持久化。
- **手动字幕**：字幕标签页表格增删改（开始/结束/文本），实时叠加预览，
  可开关烧录；手动字幕写入 EditIR，优先于转写自动生成。
- **全局音频**：音量增益（dB）与保持时长的音调（半音）进入 EditIR，
  复用单次 postprocess（pitch → volume → loudnorm）渲染。
- **多轨音频**：原声、配音、BGM 与分离 stem 可独立放置、增益、静音/独奏、
  循环、淡入淡出；`mix_audio` 用单次 filter_complex 混音，视频流保持 copy。
- **音频 lane 与波形**：时间线增加音频 lane；主素材与每个音频资产的波形
  按内容寻址缓存。
- **混音预览**：音频编辑后 600ms 防抖生成低码率混音预览并按参数缓存，
  不阻塞最终渲染；成片仍只编码一次音频。
- **人声分离（可选）**：`Separator` 协议 + 外部 Demucs 2-stem 适配器，
  独立进程生成人声/伴奏；结果按内容寻址缓存并注册为音频素材；
  主程序不打包 PyTorch，UVR/MDX 可后续接入同一协议。
- **专业界面**：深/浅色 QSS、顶部图标工具栏、统一面板/表格/滑杆样式；
  高对比度与界面缩放继续可用。
- **GUI 可视化**：revision diff 高亮（新增/删除/未变）、候选片段搜索与一键加入、
  Agent 指标面板（队列/耗时/重试/缓存/token/编码器）。
- **长视频时间线**：滚轮缩放、Shift+滚轮/中键平移、标尺、波形、缩略图轨、
  可见范围裁剪；关键帧/contact sheet 异步加载缓存。
- **字幕与大纲**：章节摘要/转写上下文、字幕叠加预览与字号/颜色/位置；
  新参数进入设置页（感知帧上限、转写 worker、静音阈值、口癖词表、字幕样式、导出预设）。
- **项目运维**：项目重命名/删除（级联清理工作区与任务记录）、替换素材、
  未保存时间线退出/切换确认、窗口几何/停靠状态/分割条/标签页恢复。
- **任务与诊断**：任务队列表（多选取消、自动刷新）、环境诊断与最近日志、
  一键打开日志目录；可选服务端提供 `GET /api/jobs/{id}/events` 与
  `GET /api/runs/{id}/events` SSE 轮询流。
- **主题与无障碍**：深色/浅色/跟随系统（当前按深色处理）主题、界面缩放、
  高对比度描边；语言切换仍未实现（English 选项禁用）。
- **策略驱动**：agents/tools/encoding/vision/workflow 由 `src/smartcut/policies/*.json`
  约束，`scripts/check_drift.py` 防止代码与配置漂移。
- **内嵌资源**：ffmpeg/ffprobe、base whisper 模型、OpenCV Haar 数据随 exe 分发。
- **数据与密钥**：数据写 `%LOCALAPPDATA%\SmartCut`；API Key 存 Windows Credential Manager。
- **版本与手工精修**：revision 列表、片段起止编辑、dry-run、渲染、产物。
- **独立导出/转码**：不进入 EditIR 的 `TranscodeService`，支持 mp4/mov/mkv、
  H.264/HEVC/AV1/ProRes、码率/分辨率/帧率/像素格式、多音轨、软字幕、元数据；
  API 与桌面“导出 / 转码…”均可调用。
- **输入规范化**：渲染默认 CFR、10bit 降级提示、可选 HDR tonemap；
  独立转码支持 autorotate/keep_10bit/tonemap。
- **音频/字幕增强**：BGM sidechain ducking、两遍 loudnorm、ASS/fontsdir、软字幕封装。
- **可选服务端**：FastAPI 仍保留为 optional extra，便于 Web/自动化，不进入桌面包。

## 开发态运行

```bash
conda activate video_edit

# 桌面 + 本地转写 + 测试依赖
python -m pip install -e ".[desktop,transcribe,dev]"

# 启动 GUI
python -m smartcut.desktop.app
```

不配置 API Key 时，Orchestrator 自动降级为分析与转写；可在“设置”中填写
DeepSeek Key（保存到 Windows Credential Manager），或开发态继续使用 `.env`。

可选人声分离（伴生 worker，不进入主程序依赖）：

```bash
# CUDA 12.6 GPU 版 PyTorch（CPU 用户可省略 --index-url）
python -m pip install torch==2.6.0+cu126 --index-url https://download.pytorch.org/whl/cu126
python -m pip install -e ".[separation]"

# 下载 htdemucs 权重到 vendor/demucs_models
python scripts/prepare_demucs_models.py

# 构建伴生 worker
python -m PyInstaller packaging/smartcut-separator.spec --noconfirm --clean
```

打包后把 `dist/SmartCut-Separator` 复制到 `dist/SmartCut/separator/`，
主程序会自动发现并调用；开发态也可在设置页填写外部 Demucs 命令。
本机 CUDA 12.6 构建验证：`torch 2.6.0+cu126 / RTX 4060`；
伴生 worker 约 4.6GB，完整 `dist/SmartCut` 约 5.6GB（GPU 版）。
未安装/未配置时，音频面板会明确提示，且不影响多轨编辑与渲染。
Demucs 代码与预训练权重的许可证需分别核对；商用分发前请做合规评估。

可选服务端：

```bash
python -m pip install -e ".[server]"
uvicorn smartcut.server.main:create_app --factory --host 127.0.0.1 --port 8000

# 运行中取消 / 缓存辅助续跑
curl -X POST http://127.0.0.1:8000/api/jobs/<job_id>/cancel
curl -X POST http://127.0.0.1:8000/api/runs/<run_id>/resume

# 结构化清单 / 成片审片
curl http://127.0.0.1:8000/api/projects/<project_id>/manifest
curl -X POST http://127.0.0.1:8000/api/projects/<project_id>/revisions/1/review-render \
  -H 'Content-Type: application/json' -d '{"goal":"检查成片","keyframe_count":6}'

# 模板与批量
curl -X POST http://127.0.0.1:8000/api/templates \
  -H 'Content-Type: application/json' \
  -d '{"name":"访谈模板","goal":"提炼主题","phase":"fast_outline"}'
curl -X POST http://127.0.0.1:8000/api/batch/orchestrate \
  -H 'Content-Type: application/json' \
  -d '{"project_ids":["p_a","p_b"],"template_id":"tpl_xxx"}'
```

`phase` 支持 `outline` / `fast_outline` / `audio_first` / `plan`。

## 性能基准

```bash
# 默认重复 2 轮，观察首轮与缓存命中后的阶段耗时
python scripts/benchmark_long_video.py --src D:\video.mp4 --repeat 2

# 启用进程池并包含转写
python scripts/benchmark_long_video.py --src D:\video.mp4 \
  --process-pool on --transcribe
```

输出 JSON 包含每阶段耗时、缓存命中、质量/人脸 engine、解码器、
转写 worker 与设备。

## 打包 Windows exe

```powershell
conda activate video_edit
python -m pip install -e ".[desktop,transcribe,vision,packaging]"

# 1. 准备内嵌资源：复制 ffmpeg/ffprobe，下载 base 模型
#    国内默认走 ModelScope；可直连 HuggingFace 时加 --source hf
python scripts/prepare_vendor.py --source modelscope

# 2. one-folder 构建
python -m PyInstaller packaging/smartcut.spec --noconfirm --clean

# 3. smoke test + 内嵌资源自检
dist\SmartCut\SmartCut.exe --smoke-test --data-dir $env:TEMP\smartcut-smoke
dist\SmartCut\SmartCut.exe --self-check --data-dir $env:TEMP\smartcut-smoke
# selfcheck.json 会记录 ffmpeg/ffprobe 版本、可用编码器、policy 加载与 whisper 模型结果
```

生成物：`dist/SmartCut/SmartCut.exe` + 资源目录。默认 one-folder（启动快、
杀软误报少）；one-file/Nuitka 可作为后续备选。

本机已验证：one-folder 构建成功；`SmartCut.exe` 约 24MB、整包约 968MB（含 OpenCV）。
`--smoke-test` 与 `--self-check` 均退出 0，内嵌 ffmpeg/ffprobe 可执行、
base whisper 模型可加载并转写、OpenCV Haar 数据与 PySceneDetect 可用；
self-check 能读取内嵌 policies 并探测 h264_nvenc/qsv/amf/libx264。
国内网络直连 HuggingFace 可能失败，
`prepare_vendor.py --source modelscope` 可正常下载模型。
OpenCV 锁定 4.x（5.x 移除了内置 Haar cascade），会明显增加包体。

打包内容：

| 资源 | 位置 | 说明 |
|---|---|---|
| ffmpeg / ffprobe | `vendor/ffmpeg/win64/` → `bin/` | 不依赖系统 PATH |
| faster-whisper 模型 | `vendor/whisper/base/` → `models/whisper/base` | 默认 base，可改 tiny/small |
| OpenCV / PySceneDetect | PyInstaller 自动收集 | 含 Haar cascade 与人脸检测 |
| PySide6 / Qt | PyInstaller 自动收集 | 含 QtMultimedia |
| 应用数据 | `%LOCALAPPDATA%\SmartCut\` | DB、workspace、logs |
| DeepSeek Key | Windows Credential Manager | 不写明文配置 |
| 策略文件 | `src/smartcut/policies/` → `_internal/smartcut/policies/` | agents/tools/encoding/vision/workflow |

## 桌面工作流

1. 新建项目 → 导入/上传素材（复制进项目工作区）。
2. 自动 probe + 生成 720p 代理，预览面板播放。
3. 输入目标，运行 Orchestrator：
   `analyze → frames → scenes → {quality ∥ faces ∥ contactsheet} ∥ transcribe
    → chapters → candidates → plan → audio → critic → qa → compile → render → verify
    → DecisionAgent`
4. 长视频可勾选“先生成大纲”：
   `Phase A（到 chapters）→ awaiting_approval → 人工确认章节
    → Phase B（规划 → Audio → Critic → QA → 渲染）`。
   - “快速大纲”只做抽帧/镜头/contact sheet → 章节，再进入 Phase B；
   - “音频优先”先转写/话题边界，再抽帧与感知，适合访谈/课程/口播。
5. 打开项目优先播放 720p 代理；渲染完成后自动切换到成片。
6. 点击“审片当前成片”检查时长/音轨/比例/字幕/黑帧模糊；配置 VLM 时
   额外做关键帧复核，只输出问题列表，不修改 IR。
7. 查看最终答案、任务账本、感知标签页、contact sheet 与章节关键帧；最新 revision 自动加载。
8. 在“版本与片段”中改起止秒数 → 保存为新版本 → dry-run → 渲染；
   可通过 revision diff 查看新增/删除/未变片段。
9. 配置 VLM 后，规划会自动使用 contact sheet + 章节关键帧/tile 的视觉摘要；
   未配置或调用失败时完全回退文本规划。
10. 需要竖版/字幕/响度时，勾选 AI 面板选项；渲染产出 9:16 视频与 SRT/VTT。
11. 运行中可取消；失败/取消后可“缓存辅助续跑”，已完成的分块/感知/LLM 结果会复用。
12. 模板与批量编排可通过可选服务端 API 使用（见下文）。
13. 源文件全程只读；所有产物在项目工作区。

## 数据与本地化

```
%LOCALAPPDATA%\SmartCut\
├── desktop.json                 # 非敏感配置
└── workspace\
    ├── smartcut.db
    ├── templates\               # 可复用编辑模板
    ├── logs\smartcut.jsonl
    └── projects\{project_id}\
        ├── media\               # 用户素材副本
        ├── cache\               # 内容寻址缓存：frames/scenes/quality/faces/
        │                        # chapters/candidates/manifest/vision_notes/
        │                        # transcripts/audio_chunks/segments/proxy/llm/
        │                        # review_frames/review_sheet
        ├── renders\r{n}\        # 成片 + captions.srt/vtt
        └── tmp\runs\{run_id}\   # 每次编排的 result.json / checkpoint
```

- 缓存键 = `source_hash + tool + params_hash`；源文件变化会自动生成新键。
- `source_hash` 在 ingest 时计算并写入 `media_assets`，重启/打开项目不再全量重算。
- 项目 bundle 的 project/revisions/runs/artifacts/perception/proxy 并行加载；
  运行列表只返回摘要，完整账本按需拉取。
- 候选检索用 SQLite FTS5（优先 trigram，中文可子串检索；不可用时回退
  unicode61 + LIKE），索引按项目与源哈希隔离。
- Segment 缓存键不含输出路径；重复渲染会直接复制已编码片段，只重跑变化的 cut。
- 大纲确认写入 `smartcut.db` 的 `outline_approvals`；Phase B 新建 run 引用快照，
  不修改 Phase A 的 ledger。
- 硬件编码由 `SMARTCUT_HARDWARE_ENCODING=auto|off` 控制；失败自动回退 libx264，
  Segment 缓存键包含编码器参数。
- 视觉摘要默认关闭；配置 `SMARTCUT_VISION_BASE_URL/MODEL` 与 `VISION_API_KEY`
  后才会发送缩放后的 contact sheet/keyframes，失败自动回退文本规划。
- `src/smartcut/policies/*.json` 是 agent/tool/encoder/vision/workflow 的机器可读约束；
  `python scripts/check_drift.py` 会检查代码与策略是否一致。
- 仅 DeepSeek HTTPS 走网络；faster-whisper 使用本地模型目录。
- ffmpeg 从 `bin/`（打包态）或 `vendor/`（开发态）解析。
- 支持 `--data-dir` / `SMARTCUT_DATA_DIR` 覆盖数据目录。
- 本地转写已用 `flite` 合成语音 + base 模型验证；转写前统一抽成 16k mono WAV，
  以 numpy 数组传入 faster-whisper，避开 PyAV 版本兼容问题。
- 长视频：音频按 `SMARTCUT_TRANSCRIBE_CHUNK_SECONDS` 分块，块结果单独缓存；
  章节按镜头/转写/时长生成；重复运行命中内容寻址缓存。
- 并发：`SMARTCUT_FFMPEG_CONCURRENCY` / `SMARTCUT_CPU_CONCURRENCY` /
  `SMARTCUT_LLM_CONCURRENCY` 控制共享公平资源池（优先级/timeout/deadline）；
  `SMARTCUT_RESOURCE_WAIT_TIMEOUT_SECONDS`（0=不限）；并行 cut 也受 ffmpeg 配额约束。
- 感知进程池：`SMARTCUT_PERCEPTION_PROCESS_POOL=auto|on|off`、
  `SMARTCUT_PERCEPTION_PROCESS_POOL_MIN_FRAMES`（默认 240）。
- GPU 解码：`SMARTCUT_HARDWARE_DECODING=auto|off`、
  `SMARTCUT_DECODE_HWACCEL`（默认 cuda）；失败自动回退 CPU。
- 转写设备：`SMARTCUT_WHISPER_DEVICE=auto|cpu|cuda`、
  `SMARTCUT_WHISPER_GPU_COMPUTE_TYPE`（默认 float16）。
- VLM 成本：`SMARTCUT_VISION_TEXT_FIRST`、`SMARTCUT_VISION_TEXT_MIN_CHARS`、
  `SMARTCUT_VISION_CHAPTER_TILES`、`SMARTCUT_VISION_SECOND_PASS_CAUTIONS`。
- 本地视觉：`SMARTCUT_VISION_PROVIDER=local`、
  `SMARTCUT_LOCAL_VISION_MODULE`、`SMARTCUT_LOCAL_VISION_BACKEND=module|onnx`、
  `SMARTCUT_LOCAL_VISION_MODEL_PATH`。
- 候选与 Critic：`SMARTCUT_CANDIDATE_TARGET_SECONDS`（默认 30）、
  `SMARTCUT_CANDIDATE_MAX`（300）、`SMARTCUT_CANDIDATE_RETRIEVAL_LIMIT`（60）、
  `SMARTCUT_CRITIC_MAX_SEGMENT_SECONDS`（90）、
  `SMARTCUT_CRITIC_QUALITY_PADDING`（0.5）。
- 去口癖：`SMARTCUT_AUDIO_FILLER_WORDS`（`um,uh,erm,嗯,呃,那个`）、
  `SMARTCUT_AUDIO_FILLER_PADDING`（0.05）、
  `SMARTCUT_AUDIO_MIN_SEGMENT_SECONDS`（0.4）。
- 分块转写：`SMARTCUT_TRANSCRIBE_WORKERS`（0=auto；CPU+clone 最多 2）、
  `SMARTCUT_TRANSCRIBE_SILENCE_RMS`（0.002）、
  `SMARTCUT_TRANSCRIBE_SILENCE_MIN_SECONDS`（0.5）。
- 感知：`SMARTCUT_PERCEPTION_MAX_FRAMES`（默认 1200，0=不限）；
  质量/人脸按帧并行，并按镜头切点/质量风险/话题变化/章节边界分配帧预算，
  支持感知哈希去重；长任务可选进程池。
- 渲染：fit + 烧录字幕 + loudnorm 合并为单个 `postprocess` 调用；
  只做响度时保持视频流 copy。

## 测试

```bash
conda activate video_edit
python -m pytest -q
```

当前结果：**272 collected，270 passed + 2 skipped（live）**。
默认测试不访问网络：契约、golden、ffmpeg 工具链、代理生成、
OpenCV 镜头/质量/人脸/contact sheet、9:16、字幕映射与烧录、响度实测、
内容寻址缓存、分块转写、章节化、候选片段与 FTS5 检索、Critic 局部重规划、
Segment 缓存与并行 cut、IR diff、两阶段大纲、硬件编码参数与回退、
视觉摘要/降级、AudioAgent/QA、调度器非阻塞、source_hash 持久化、感知缓存、
policy 漂移、旧库迁移、取消与续跑、Orchestrator 任务图/端到端、
项目重命名/删除级联与活动任务保护、任务列表/SSE 事件/诊断日志、
队列/诊断/主题/设置持久化 GUI、手动字幕覆盖与无转写字幕、
音频增益/保持时长变调与 postprocess 滤镜、播放器音量/静音、
多轨契约/混音 argv/真实 ffmpeg 渲染、音频资产导入与删除级联、
假 Demucs 适配器与 stem 缓存、API、桌面路径、offscreen GUI smoke，
以及使用内嵌 base 模型的本地 ASR
（没有 `vendor/whisper/base` 时自动 skip）。

新增回归覆盖：抽帧/缩略图单次解码、自适应选帧与感知哈希去重、
VLM 文本优先/tile/二次复核/single-flight、VideoManifest 增量合并与缓存、
章节增量分析、快速大纲、公平资源池、进程池、GPU 解码回退、
转写 GPU 策略、性能基准、成片审片、音频优先、模板/批量、
本地视觉插件。
真实 DeepSeek 联调默认跳过：

```bash
SMARTCUT_LIVE_DEEPSEEK=1 python -m pytest -q -m live
```

## 目录

```
src/smartcut/
  contracts/      # RationalTime / MediaFacts / EditIR / Manifest / Template / ToolCall
  perception/     # ffprobe、转写、感知、候选、manifest、进程池
  llm/            # DeepSeek/Vision 客户端、single-flight、本地视觉适配器
  planner/        # EditIR 规划（章节 + 候选 + 视觉上下文）
  compiler/       # 纯函数 lowering、字幕/包装、编码参数、IR diff
  runner/         # 沙箱、ffmpeg 工具、编码/解码探测、ffprobe 校验
  orchestrator/   # 任务图、子智能体、Critic、大纲/快速大纲/音频优先
  policies/       # agents/tools/encoding/vision/workflow JSON 约束
  desktop/        # PySide6 GUI、大纲确认、审片入口、控制器、资源路径
  server/         # 可选 FastAPI、缓存、FTS5、模板、批量、大纲快照
  benchmark.py    # 长视频阶段性能基准
docs/             # 需求/可行性 + INVARIANTS/AGENT_PROTOCOL/MEDIA_PIPELINE/
                  # VISION/PHASE3/PERFORMANCE/GUI/OPTIMIZATION_PLAN
scripts/          # prepare_vendor / smoke_desktop / check_drift / benchmark_long_video
packaging/        # PyInstaller spec / Windows 构建脚本
tests/            # 核心 + 桌面 + live
```

## 许可与注意

- 当前 ffmpeg 构建可能带 GPL 组件；商用分发需替换为 LGPL 构建或做合规评估。
- H.264 编码涉及专利许可；内部使用风险较低，商用需评估。
- PySide6 为 LGPL；请保留相应许可与动态链接方式。
- 首次构建需联网下载 PySide6/faster-whisper 与 base 模型；之后完全离线运行。
- 当前时间线支持片段拖拽/裁剪/排序、音频 lane 与多轨混音；
  视频转场、变速/关键帧动画、OTIO/FCPXML 交换、WebSocket 推送仍为后续
  里程碑。可选服务端已提供仅轮询数据库的 SSE 事件流，桌面端仍使用 Qt 信号。
