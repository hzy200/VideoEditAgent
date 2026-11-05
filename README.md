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
- **离线规则规划**：无 DeepSeek Key 时由确定性 `RulePlanner` 按 goal 时长、候选
  score、章节/镜头边界与质量区间产出 EditIR，完整 DAG 仍可跑通；结果明确标注
  `source=rule`，不冒充 AI 规划。可用 `SMARTCUT_PLANNER_OFFLINE_FALLBACK=false`
  恢复“只分析”行为。
- **选段质量评估**：`scripts/evaluate_selection.py` 对比人工 EDL 与规划区间，
  输出 interval/segment IoU、覆盖率、重复率与时长偏差；CI 用 `-m eval` 跑规则
  模式并设 F1 下限。
- **每任务独立工作区**：文件复制到 `workspace/projects/{id}/`，源文件只读。
- **本地媒体能力**：ffprobe、cut/concat、代理预览、转写（faster-whisper）。
- **OpenCV 感知**：PySceneDetect 镜头切分、黑/静/糊/重复帧、人脸锚点 +
  centroid/IoU 跟踪、HSV 镜头聚类、contact sheet；证据视图按内容路由，
  可选独立 worker OCR 识别屏幕文字。
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
- **VLM 成本优化**：默认使用 top-K 视觉证据视图（Txx 标签），文本优先、
  章节 2x2 tile、单图 sha256 补算、single-flight、低置信度章节二次复核；
  未配置/失败自动 text-only，可用 `SMARTCUT_VISION_USE_EVIDENCE=false` 回退旧路径。
- **硬件加速**：硬件编码 NVENC/QSV/AMF；GPU 解码（hwaccel）失败自动回退 CPU；
  OpenCV 长任务可选 spawn 进程池；whisper `device=auto` 懒检测 CUDA，
  GPU 强制单 worker。
- **渲染审片**：对成片检查时长/音轨/比例/字幕/黑帧模糊，可选 VLM 关键帧复核，
  只输出问题与评分，不修改 EditIR。
- **模板与批量**：编辑模板（目标、phase、9:16/字幕/响度/去口癖）可复用；
  批量编排多个项目并汇总成功/失败与产物。
- **Proxy-first 预览**：打开项目先播放 720p 代理，成片完成后再切换。
- **多模态视觉摘要**：视觉证据视图（或旧 contact sheet + 章节 keyframes/tile）
  送入可配置 OpenAI 兼容 VLM，生成带 `evidence_refs` 的视觉 notes 注入 Planner；
  未配置或失败时自动降级为文本规划。
- **可选本地视觉**：`vision_provider=local` 通过外部插件模块调用本地 VLM
  或 ONNX 推理，主程序不打包/加载 torch；依赖或模型缺失时自动 text-only。
- **AudioAgent**：按词级时间戳去口癖并提交修正 revision。
- **SemanticQAAgent**：LLM/规则审片，输出问题、score 与警告，不直接改时间线。
- **长视频性能**：source_hash 持久化、项目 bundle 并行加载、依赖驱动调度、
  视觉摘要双层缓存、编码器试编码、感知按帧并行与场景自适应采样、
  fit+字幕+响度单次编码；公平资源池（优先级/timeout/deadline）、
  可选 OpenCV 进程池、GPU 解码与转写策略。
- **可观测性**：每次 run 记录按 agent 的队列等待/耗时/重试、缓存命中率、
  LLM token、编码器、VLM 图片/字节/token、OCR 条目数、解码器、分析 engine、
  `measure_backend`、抽帧/分析/丢弃帧数与阶段耗时；FTS top 内存排序 + 缓存；
  工具审计每 20 条批量写。
- **性能基准**：`scripts/benchmark_long_video.py` 按阶段输出耗时、缓存命中、
  分析 engine、转写 worker/设备；`scripts/benchmark_evidence_render.py`
  对比 CPU/GL 证据渲染耗时（支持 `--kind` / `--assert-not-slower`）。
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
- **专业界面**：深色专业 NLE 风格（深石墨分层表面、青蓝强调、胶囊工作区
  导航、图标分组工具栏、传输条、主/次/危险按钮层级）＋精修浅色主题；
  时间线/曲线/证据画布固定深色剪辑台；高对比度与界面缩放继续可用。
- **GUI 可视化**：revision diff 高亮（新增/删除/未变）、候选片段搜索与一键加入、
  Agent 指标面板（队列/耗时/重试/缓存/token/编码器）。
- **可解释选段**：时间线「依据」列与 Inspector 展示每段 rationale；候选 tooltip
  显示 score/tags/质量/人脸/镜头边界。
- **视觉证据层（M0）**：候选窗口按 2fps 抽帧并解析真实 PTS，生成带
  `T01` 编号、`frame_no`/`pts`、render/semantic hash 的 `VisualEvidence`；
  dHash 去重、top-K 图片/token 硬预算、CPU Pillow 参考渲染、原子目录缓存，
  并在 run metrics 中记录 extract/measure/render 耗时与缓存命中。
- **VLM/Planner 证据引用（M0.6）**：默认 VLM 输入优先使用证据视图
  （`SMARTCUT_VISION_USE_EVIDENCE=false` 可回退旧 contact sheet），prompt 插入
  `[Txx|秒]` 证据时间线与转写文本；`evidence_refs` 只能引用真实标签，未知标签
  直接拒绝；Planner rationale 同样校验 `Txx`，避免引用不存在的帧编号。
- **Qt OpenGL 证据渲染（M1）**：桌面使用每线程离屏 QOpenGLContext/FBO +
  QOpenGLPaintDevice 合成 tile/编号/bbox；`render_hash` 不含 backend，CPU/GL
  用 `semantic_hash` 容差比对；无 GL/RDP/驱动异常自动回退 Pillow，并记录
  `backend=cpu`。`--self-check` 包含 64×64 离屏渲染与 CPU 语义比对。
- **更多证据视图（M2）**：帧差/光流双通道、flow_x/flow_y 条带、bbox 叠层、
  ROI 回原素材高分辨率裁切、ffmpeg `codecview` 残差可视化；按 CPU/GL 分档
  限制图片与 tile 尺寸；“视觉证据”面板展示缩略图、Txx 时间码并可点击定位；
  时间线只读叠层显示证据点、静音区与候选窗口。
- **证据路由与多视图（P0）**：`policies/vision.json` 按目标关键词与感知信号
  选择主视图 + 最多 2 个补充视图（口播→bbox+ROI、运动→帧差+光流等）；
  多视图共享一次候选窗口与原始帧抽帧，Txx 标签全局不相交；ROI 按
  `measurements.score` 选最异常时刻；二次复核对 flagged 章节换 kind/ROI。
- **测量事实注入 Planner（P0.3/P0.4）**：`d_hist/flow/dir/face/score/risk/
  cam/subj/motion` 进入 VLM 证据时间线；章节级 `visual_facts`
  （face_coverage/mean_d_hist/motion_level/camera_motion/quality_risks）
  注入 Planner，默认限制前 40 章。
- **屏幕文字 OCR（P1）**：cv2 形态学梯度/轮廓筛选候选区，OCR 走独立 worker
  （可选 `.[ocr]`，主进程不 import 重依赖）；缓存键含
  `source_hash + frame_sha256 + worker_version + policy_version`；
  结果按时间对齐章节 note / Planner，不写 IR。
- **主体跟踪与平滑（P1）**：centroid+IoU 关联输出 `track_id` 与出现区间，
  同一 track 锚点 EMA 平滑，改善 9:16 裁切稳定性。
- **相机/主体运动拆分（P1）**：RANSAC 仿射拆出 `camera_mag/subject_mag`，
  分类 static/pan/tilt/zoom，供 Planner 识别 B-roll、推拉摇移与抖动。
- **镜头聚类（P1）**：HSV 直方图相关性聚类，章节只在视觉簇边界处切分，
  避免同一页 PPT/录屏被内部切点拆成多章。
- **统一基础指纹（P1）**：dHash/直方图收口到 `perception/view/fingerprint.py`，
  `dhash:v2` 与 engine 进入证据/质量/候选缓存键与 evidence meta。
- **批量渲染 API（P2）**：`render_many/synthesize_many` 对同一 `MeasureResult`
  的多个请求复用 context 与解码图片缓存；多视图证据每种 kind 独立测量，不走
  该入口，共享的是候选窗口与抽帧。
- **独立 GpuMeasurer（P2）**：与 renderer 解耦，只输出数值 `MeasureResult`；
  CPU 等价路径 + 数值容差比对 + 失败回退，`measure_backend` 不参与
  `render_hash`；仅真正加速的后端可把采样提升到 10–30fps（当前 OpenCV CUDA
  绑定有限，CUDA 为 `accelerated=False` 的占位实现，默认 CPU；kernel 可替换
  而不改变契约）。
- **反馈闭环**：保存 revision 时把被删除片段写入 `segment_reject`，偏好统计
  排除被拒标签；「我的偏好」面板显示样本数/中位片段/保留率/top tags 与
  「按我的节奏规划」开关。
- **4 工作区 + 场景预设**：剪辑 / 素材与感知 / 包装 / 运行与诊断；内置
  口播切片、访谈精华、课程章节 3 个 `EditTemplate` 预设。
- **平台导出预设**：reels/douyin 1080x1920、youtube 1920x1080，可写入
  EditIR 或导出对话框；copy 模式会明确拒绝需要裁切的预设。
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
- **版本与手工精修**：导入后自动生成全片单段基线，时间线可立即分割/裁剪/
  拖动/删除；revision 切换、起止编辑、dry-run、渲染、产物。
- **即时预览**：导入后先用源文件播放并显示时长，代理后台生成完成后无感切换，
  不再等待整段代理。
- **视频理解问答**：AI 面板“仅理解并回答”只读转写/章节/媒体事实，输出文字
  总结或回答问题，不创建 revision、不渲染、不改时间线；无 LLM 时规则降级。
- **多素材 bin / NLE 交互**：项目 bin 保留多个素材并可拖入时间线；时间线按
  输出时间/ripple 绘制，拖动/裁剪支持磁性吸附（Alt 关闭），在相邻片段边界
  拖拽创建/调整转场（9+ 种 xfade，可选音频曲线）；多源时间线按
  `source_facts` 编译，逐 clip 统一分辨率/fps 并为无音轨素材补静音后拼接；
  视频素材可一键添加画中画并写入 V2+ 叠加轨，预览区可拖拽/缩放 PiP 框、
  用关键帧编辑器逐点编辑/删除并做 scale/overlay/alpha 插值；绝对输出时间线
  支持空隙/多轨 `timeline_start`、V1 xfade 与 V2+ alpha 转场，V2+ 音频与
  主轨混音；轨道头支持 M/S/L；由 `timeline_compose` 合成；“预览成片”按
  草稿渲染 480p 时间线预览并连续播放。
- **独立导出/转码**：不进入 EditIR 的 `TranscodeService`，支持 mp4/mov/mkv
  以及音频容器 mp3/m4a/wav/flac，H.264/HEVC/AV1/ProRes、码率/分辨率/帧率/
  像素格式、多音轨、软字幕、元数据；API 与桌面“导出 / 转码…”均可调用。
- **媒体修复与压缩**：诊断后做重封装、CFR 重编码修时间戳/卡顿、音画起始同步、
  轻度锐化；GUI“包装 → 媒体工具”提供导入/诊断/修复/转换/压缩按钮，可先复制
  无法 probe 的损坏文件再修复；AI 对话框可对当前选中源用提示词触发（含
  `POST /media/operation` 一键工作流）；所有产物写入 `exports/` 或
  `repairs/`，源文件只读。
- **输入规范化**：渲染默认 CFR、10bit 降级提示、可选 HDR tonemap；
  独立转码支持 autorotate/keep_10bit/tonemap。
- **音频/字幕增强**：BGM sidechain ducking、两遍 loudnorm、ASS/fontsdir、软字幕封装。
- **可选服务端**：FastAPI 仍保留为 optional extra，便于 Web/自动化，不进入桌面包。
- **工程门禁**：GitHub Actions（Windows + Python 3.12 + ffmpeg）跑 pytest、
  `check_drift.py`、规则选段 `-m eval` 与桌面 smoke；`schema_meta` v1→v4
  迁移、旧库幂等测试、缓存 prune、日志轮转、工具审计保留期与文档路径守卫。

## 开发态运行

```bash
conda activate video_edit

# 桌面 + 本地转写 + 测试依赖
python -m pip install -e ".[desktop,transcribe,dev]"

# 可选屏幕文字 OCR（独立 worker；主程序不 import OCR 引擎）
python -m pip install -e ".[ocr]"

# 启动 GUI
python -m smartcut.desktop.app
```

不配置 API Key 时，Orchestrator 自动降级为分析与转写；可在“设置”中填写
DeepSeek Key（保存到 Windows Credential Manager），或开发态继续使用 `.env`。

屏幕文字 OCR 默认关闭；安装 `.[ocr]`（RapidOCR）或系统 tesseract 后设置
`SMARTCUT_OCR_ENABLED=true`。OCR 在独立 worker 进程加载，引擎缺失/超时/
取消时自动降级，不阻断视觉摘要与规划。

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

证据渲染基准：

```bash
# 需要桌面/Qt 环境；无 GL 时自动记录 backend=cpu
python scripts/benchmark_evidence_render.py --tiles 9 --repeat 3 \
  --kind temporal_diff --assert-not-slower
```

GUI 端到端探针（真实 MainWindow + 合成素材）：

```bash
QT_QPA_PLATFORM=offscreen python scripts/gui_workflow_probe.py
```

输出窗口构造/显示、项目打开、标签切换、时间线编辑、候选检索、证据生成、
编排与渲染各阶段耗时；同时检查 open_project/proxy/audio_waveforms 是否去重、
无 PermissionError。耗时随机器与缓存状态变化。

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
| PySide6 / Qt | PyInstaller 自动收集 | 含 QtMultimedia / QtOpenGL（证据渲染） |
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
7. 查看最终答案、任务账本、感知/视觉证据页、contact sheet 与章节关键帧；
   “视觉证据”可选择帧差/光流/bbox/ROI/codec 残差并点击定位；最新 revision 自动加载。
8. 在“版本与片段”中改起止秒数 → 保存为新版本 → dry-run → 渲染；
   可通过 revision diff 查看新增/删除/未变片段。
9. 配置 VLM 后，规划默认使用证据视图与 `Txx` 证据时间线；按
   `policies/vision.json` 路由主视图 + 补充视图，多视图标签全局唯一，
   ROI/二次复核按 measurements 选最异常时刻；VLM/Planner 只能引用真实标签。
   未配置或调用失败时完全回退文本规划；
   `SMARTCUT_VISION_USE_EVIDENCE=false` 可回退旧 contact sheet/keyframes。
   开启 OCR 后，屏幕文字按时间注入章节 note/Planner。
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
        │                        # evidence（view.png + evidence.json）/
        │                        # ocr_frame/screen_text/
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
  后才会发送缩放后的证据视图/contact sheet/keyframes，失败自动回退文本规划。
- 视觉证据：`SMARTCUT_VIEW_EVIDENCE_ENABLED`、`SMARTCUT_VIEW_MAX_IMAGES`、
  `SMARTCUT_VIEW_MAX_WINDOWS`、`SMARTCUT_VIEW_FPS`、`SMARTCUT_VIEW_TILE_*`、
  `SMARTCUT_VIEW_RENDERER=auto|cpu`、`SMARTCUT_VISION_USE_EVIDENCE`；
  路由策略见 `policies/vision.json` 的 `routing`，缓存键含 source_hash +
  policy_version + 路由版本 + label_start/dedupe/fps + fingerprint 版本/engine
  + 渲染常量；`evidence.json` 记录 `render_hash`/`semantic_hash`/backend。
- 多视图：主视图 + 最多 2 个补充视图共享一次抽帧；Txx 全局唯一；
  ROI 按 measurements.score 选最异常时刻；二次复核换 kind/ROI。
- 屏幕文字 OCR：`SMARTCUT_OCR_ENABLED`、`SMARTCUT_OCR_ENGINE=auto|rapidocr|
  tesseract`、`SMARTCUT_OCR_MAX_FRAMES`、`SMARTCUT_OCR_MIN_CONFIDENCE`、
  `SMARTCUT_OCR_MIN_STABILITY`、`SMARTCUT_OCR_REGION_MAX_PER_FRAME`、
  `SMARTCUT_OCR_WORKER_TIMEOUT_SECONDS`、`SMARTCUT_OCR_POLICY_VERSION`；
  逐帧缓存键 = source_hash + frame_sha256 + worker_version + policy_version。
- 人脸跟踪/聚类：`SMARTCUT_FACE_TRACKING_ENABLED`、
  `SMARTCUT_FACE_TRACK_IOU_THRESHOLD`、`SMARTCUT_FACE_TRACK_MAX_GAP_SECONDS`、
  `SMARTCUT_FACE_SMOOTHING_ALPHA`、`SMARTCUT_CHAPTER_CLUSTERING_ENABLED`、
  `SMARTCUT_CHAPTER_CLUSTER_SIMILARITY`；参数进入对应缓存键。
- GPU 测量（可选）：`SMARTCUT_GPU_MEASURE_ENABLED`、
  `SMARTCUT_GPU_MEASURE_BACKEND=cpu|auto|cuda`、`SMARTCUT_GPU_MEASURE_FPS`
  （≤30）、`SMARTCUT_GPU_MEASURE_TOLERANCE`；失败回退 CPU 并记录
  `measure_backend`。
- 缓存与日志：`SMARTCUT_CACHE_MAX_MB` 只淘汰可重算 kind（frames/scenes/
  quality/faces/contactsheet/llm/vision/transcript/audio_chunks/evidence/
  ocr_frame/screen_text），segments/renders/manifest/result 永不淘汰；
  日志 10MB×3 轮转；
  `SMARTCUT_TOOL_AUDIT_RETENTION_DAYS` 清理过期工具审计。
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
- 规划兜底：`SMARTCUT_PLANNER_OFFLINE_FALLBACK`（默认 true；关闭后无 Key 只分析）。
- 偏好作用域：`SMARTCUT_PREFERENCE_SCOPE=source|project`（桌面「我的偏好」开关同步）。
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

当前结果：**563 collected，554 passed + 9 skipped（live/CI 专项/本地模型/无 GL）**。
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

P0–M2 新增回归覆盖：

- 工程门禁：CI 环境二进制守卫、schema_meta 迁移与旧库幂等、缓存 prune、
  日志轮转、工具审计清理、文档路径守卫；
- 离线规划：RulePlanner 目标时长/质量剔除/边界吸附、HybridPlanner 回退、
  无变化 replan、EDL IoU/F1 门禁；
- P2：rationale 契约与 GUI 展示、`segment_reject` 偏好排除、
  4 工作区/场景预设、reels/douyin/youtube 画幅预设；
- 视觉证据：VisualEvidence 契约/hash、PTS 抽帧、top-K 预算、CPU 证据缓存、
  VLM/Planner `evidence_refs` 校验、QtGL/CPU 语义与性能、M2 多视图、
  EvidencePanel 与时间线只读叠层。

P0–P2 语义/证据新增回归覆盖：

- 路由/多视图：`vision.json` 路由、多视图标签不相交、一次抽帧、ROI 按
  measurements.score 选最异常时刻、二次复核换 kind/ROI；
- 测量/Planner：`d_hist/flow/dir/face/score/cam/subj/motion/risk`、
  `visual_facts` 限流与 `chapter_ids` 过滤、缺失值不补造；
- OCR：候选区检测、时间稳定性聚合、逐帧内容缓存、章节 note/Planner 注入、
  worker 失败降级；
- 跟踪/运动拆分/聚类：`track_id`/出现区间、EMA 平滑、camera/subject 拆分、
  HSV 镜头聚类；
- 统一指纹：`dhash:v2`/engine、Pillow fallback、CPU/GL 语义比对；
- 渲染/测量：`render_many/synthesize_many`、GpuMeasurer CPU 等价/容差/回退、
  `measure_backend` 不参与 `render_hash`；
- GUI 端到端：真实 MainWindow 打开项目、切换全部工作区、候选检索、证据生成、
  时间线拆分/撤销、离线编排、revision 加载与真实渲染；
- 前端并发：`open_project`/`proxy`/`audio_waveforms` 单飞，主素材波形复用
  `build_audio_waveforms`，避免重复 ffmpeg 与 Windows 原子替换竞争。

CI（`.github/workflows/ci.yml`）在 windows-latest 安装
`.[dev,desktop,transcribe,vision,server]`（`vision` 是测试收集期必需）并执行：

```bash
python -m pytest -q
python -m pytest -q -m eval
python scripts/check_drift.py
python -m smartcut.desktop.app --smoke-test --data-dir "$env:TEMP\smartcut-smoke"
```

真实 DeepSeek 联调默认跳过：

```bash
SMARTCUT_LIVE_DEEPSEEK=1 python -m pytest -q -m live
```

## 目录

```
src/smartcut/
  contracts/      # RationalTime / MediaFacts / EditIR / Manifest / Template /
                  # ToolCall / visual_evidence / screen_text / export_presets
  perception/     # ffprobe、转写、感知、候选、manifest、进程池、镜头聚类、
                  # 屏幕文字候选区
    view/         # VisualEvidence 选择/测量/hash/CPU 渲染/多视图/预算/
                  # fingerprint/路由/GpuMeasurer/批量渲染
  ocr_worker.py   # OCR 独立 worker 入口；重依赖只在该进程加载
  llm/            # DeepSeek/Vision 客户端、single-flight、本地视觉适配器
  planner/        # DeepSeek/Hybrid/Rule Planner、视觉上下文、EDL 评估
  compiler/       # 纯函数 lowering、字幕/包装、编码参数、IR diff
  runner/         # 沙箱、ffmpeg 工具（含 extract_evidence_frames）、探测/校验
  orchestrator/   # 任务图、子智能体、Critic、大纲/快速大纲/音频优先
  policies/       # agents/tools/encoding/vision/workflow JSON 约束
  desktop/        # PySide6 GUI、大纲确认、审片入口、控制器、资源路径
    view/         # Qt OpenGL 离屏证据渲染器
  server/         # 可选 FastAPI、缓存、FTS5、模板、批量、大纲快照
    services/     # 业务编排 + 证据/OCR 服务/缓存/metrics
  benchmark.py    # 长视频阶段性能基准
docs/             # 需求/可行性 + INVARIANTS/AGENT_PROTOCOL/MEDIA_PIPELINE/
                  # VISION/PHASE3/PERFORMANCE/GUI/OPTIMIZATION_PLAN
scripts/          # prepare_vendor / smoke_desktop / check_drift /
                  # benchmark_long_video / evaluate_selection /
                  # benchmark_evidence_render
packaging/        # PyInstaller spec / Windows 构建脚本
tests/            # 核心 + 桌面 + live + eval + evidence/GL + 路由/OCR/
                  # 跟踪/运动拆分/聚类/指纹/GpuMeasurer
```

## 许可与注意

- 当前 ffmpeg 构建可能带 GPL 组件；商用分发需替换为 LGPL 构建或做合规评估。
- H.264 编码涉及专利许可；内部使用风险较低，商用需评估。
- PySide6 为 LGPL；请保留相应许可与动态链接方式。
- 首次构建需联网下载 PySide6/faster-whisper 与 base 模型；之后完全离线运行。
- 当前时间线支持片段拖拽/裁剪/排序、音频 lane 与多轨混音，并已落地
  视频转场、变速（走时长模型与 `compose`）、图片/文字水印与手动 delogo。
  真正尚未实现的是 OTIO/FCPXML 交换、关键帧动画与 WebSocket 推送；
  可选服务端已提供仅轮询数据库的 SSE 事件流，桌面端仍使用 Qt 信号。
