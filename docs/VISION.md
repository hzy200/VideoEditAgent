# 多模态视觉摘要

## 目标

把全局 contact sheet 与章节关键帧交给一个 OpenAI 兼容 VLM，生成章节级视觉摘要，
再注入纯文本 Planner。VLM **不直接生成 EditIR**，避免非文本模型破坏结构化契约。

## 配置

```bash
SMARTCUT_VISION_BASE_URL=https://your-vlm.example/v1
SMARTCUT_VISION_MODEL=your-vl-model
VISION_API_KEY=...
SMARTCUT_VISION_JSON_MODE=false
SMARTCUT_VISION_MAX_IMAGES=7
SMARTCUT_VISION_IMAGE_MAX_EDGE=1024
SMARTCUT_VISION_IMAGE_MAX_BYTES=4194304
SMARTCUT_VISION_TOTAL_MAX_BYTES=12582912
SMARTCUT_VISION_MAX_KEYFRAMES_PER_CHAPTER=2
# M0–M2 视觉证据（默认优先，失败回退旧 contact sheet）
SMARTCUT_VISION_USE_EVIDENCE=true
SMARTCUT_VIEW_EVIDENCE_ENABLED=true
SMARTCUT_VIEW_RENDERER=auto
SMARTCUT_VIEW_MAX_IMAGES=7
SMARTCUT_VIEW_MAX_WINDOWS=4
SMARTCUT_VIEW_FPS=2
SMARTCUT_VIEW_TILE_WIDTH=384
SMARTCUT_VIEW_TILE_HEIGHT=216
```

- 未配置 base_url/model 时完全禁用，不影响文本规划，也不产生 warning。
- 若 base_url 与 DeepSeek 相同，可复用 DeepSeek Key；否则必须单独提供
  `VISION_API_KEY`，避免把 DeepSeek Key 发给第三方。
- 桌面设置可写入视觉 Base URL/模型，Key 存 Windows Credential Manager。

## 输入选择

证据路径（`SMARTCUT_VISION_USE_EVIDENCE=true`）优先：

1. `policies/vision.json` 的 `routing` 按目标关键词与感知信号选择内容类型；
   每种类型给出 1 个主视图 + 最多 2 个补充视图（如口播 → `bbox_overlay` +
   `roi_crop`，运动 → `temporal_diff` + `flow_strip`）。
2. 主视图必须保留；补充视图按 `vision_max_images` / `vision_total_max_bytes`
   全局预算追加，按 sha256 去重。
3. 每个视图的 `Txx` 标签区间由服务层顺序分配，**本次全部视图必须不相交**；
   `evidence_refs` 白名单取全部视图标签的并集，但跨视图同名标签不得视为合法。
4. 同一批视图共享一次候选窗口选择与原始帧抽帧；`roi_crop` 按不变量回原素材
   单独抽高分辨率 ROI，`codec_residual` 单独做 codecview 抽帧。
5. 证据时间线附带测量事实：`d_hist`、`flow_mag`、`flow_dir_deg`、`score`、
   `face`（人脸框面积占比）、`cam/subj/motion`（相机/主体运动）、`risk`
   （质量标记）；缺失字段不补造，首帧 `d_hist=1.0` 占位值不注入。
6. 路由策略版本、全部视图 id/render_hash/semantic_hash、measurement 摘要与
   屏幕文字摘要进入视觉结论缓存键。
7. 证据不可用时回退旧 contact sheet/keyframes 路径。

旧路径：

1. 全局 contact sheet 最多 1 张（来自 `cache/contactsheet/*.jpg`）。
2. 选中章节各最多 2 张 keyframes（`ChapterPlan.keyframes`）。
3. 总数 ≤7；按 sha256 去重；长边缩放到 ≤1024，统一转 JPEG q82。
4. 单图/总字节上限遵循 `src/smartcut/policies/vision.json` 与 Settings。
5. 只用 workspace 内路径；缺失/损坏图片跳过，不阻断。

## 输出

VLM 返回：

```json
{
  "overall": "整体视觉摘要",
  "chapters": [
    {
      "chapter_id": "ch_0001",
      "summary": "人物近景讲解",
      "visual_tags": ["人物", "口播"],
      "cautions": ["轻微模糊"]
    }
  ]
}
```

`ChapterVisualNote` / `VisualNotes` 契约负责校验；非法 JSON 允许有限修复。
结果进入 Planner 上下文的 `visual_notes`，提示词要求只作辅助，不得输出路径/坐标。

服务端还会按章节聚合 `visual_facts`：`view_kinds`、`face_coverage`、
`mean_d_hist`、`motion_level`、`camera_motion`、`quality_risks`。
Planner 上下文单独注入该段，默认限制前 40 章；只含数值/标签，不含坐标、
路径或时间线操作。开启 OCR 后，`ChapterVisualNote.screen_text` 与
`visual_notes.screen_text` 也会进入 Planner。

## 缓存与降级

- 文本结论缓存键：source_hash + 模型 + base_url + prompt_version + goal +
  章节 id + 图片 sha256 + 路由版本 + 全部视图哈希 + measurement 摘要 +
  OCR 摘要。
- 证据缓存键另含 kind/tile_index/roi_target/label_start/dedupe/fps/
  fingerprint 版本与 engine；命中缓存不重复请求 VLM。
- 超时/HTTP 错误/JSON 失败/Pillow 缺失/图片缺失 → 返回 warning，使用纯文本规划。
- 视觉调用使用独立 `VisionClient`，与 DeepSeek 文本客户端共享 HTTP/缓存/资源池模式。

## 隐私

- 只有配置了 VLM 才会发送图片；默认关闭。
- 发送的是缩放后的 JPEG，不包含源文件路径。
- 日志不记录 base64 图片或 API Key。

## 视觉证据层（M0）

- 证据窗口从镜头切点、质量风险、章节边界、转写与粗扫帧生成；
- 每个候选窗口按 `SMARTCUT_VIEW_FPS` 抽帧；GPU 测量后端可用且启用时按
  `SMARTCUT_GPU_MEASURE_FPS`（≤30）提高采样密度；使用
  `extract_evidence_frames` 解析 showinfo 的真实 PTS，并受图片/token 硬预算约束；
- CPU 参考渲染输出带 `T01` 编号的 contact sheet，重复帧先经统一 dHash 去除；
- 产物包含 `evidence.json`（TileMap/测量/meta/render_hash/semantic_hash）与
  `view.png`，按 `source_hash + policy_version + label_start + dedupe + fps +
  fingerprint 版本/engine + 渲染常量` 内容寻址缓存；
- 失败或未抽帧时返回 warning，不阻断规划与渲染；M0 不直接写入 IR。
- 默认 `SMARTCUT_VISION_USE_EVIDENCE=true`；设为 false 可回退旧 contact sheet。启用时 VLM 输入改为证据视图；
  路由按 `vision.json` 输出主视图 + 补充视图，多视图标签区间不相交；
  prompt 增加 `[Txx|秒]` 证据时间线与转写文本，要求输出 `evidence_refs`；
  服务端按本次全部 TileMap 标签并集校验，未知标签直接拒绝（不静默过滤）。
- Planner 上下文注入 `visual_notes.chapters[].evidence_refs`；rationale 中引用的
  `Txx` 必须存在于该集合，否则规划响应被拒绝。
- 二次复核：对 flagged 章节重新做 chapter-scoped 证据选择，换用不同 kind
  （`contact_sheet`/`bbox_overlay`/`codec_residual`）+ `roi_crop`；ROI 按
  overview 的 `measurements.score` 选最异常时刻，再用时间定位原素材帧，
  避免 dedupe 后 `tile_index` 与原始帧列表错位，不固定 tile 1。
  复核视图标签继续使用不相交区间，不重复发送主视图。

## Qt OpenGL 离屏渲染（M1）

- `desktop/view/render_qtgl.py` 使用每线程 `QOffscreenSurface +
  QOpenGLContext + QOpenGLFramebufferObject`，由 `QOpenGLPaintDevice + QPainter`
  绘制 tile、编号与 bbox；shader 不计算光流。
- 桌面 `view_renderer=auto` 注入 GL；server/CI 无注入时使用 CPU Pillow。
- 上下文创建/绑定/驱动失败由服务层捕获，回退 CPU 并记录
  `backend=cpu`、`renderer_fallback=true`。
- `render_hash` 不含 backend；CPU/GL 用分块 dHash `semantic_hash` 比对
  （≤6/64，通道均值差 ≤3/255）。
- `--self-check` 执行 64×64 离屏渲染与 CPU 语义比对；不可用仅标记 degraded。
- `scripts/benchmark_evidence_render.py` 输出 CPU/GL 渲染耗时，支持
  `--assert-not-slower` 与 `--kind`。

## 更多证据视图（M2）

- `temporal_diff`：当前帧 + 帧差 + `hsv_hue_v1` 光流 + flow_x/flow_y 双通道；
- `flow_strip`：沿轨迹堆叠 flow_x / flow_y / hue 三条带；
- `bbox_overlay`：人脸/主体框叠层；
- `roi_crop`：根据 tile 时间回原素材重新抽高分辨率帧，按 bbox 扩边裁切；
- `codec_residual`：ffmpeg `codecview=mv=pf+bf+bb` 可视化，仅作证据，不输出数值 MV；
- CPU/GL 分档预算：CPU 限制 tile 数与像素尺寸，GL 使用配置上限；
- `EvidencePanel` 展示缩略图/Txx/时间码并点击定位；时间线只读叠加证据点、
  静音区与候选窗口。

## 屏幕文字 OCR（P1）

- 候选区由 cv2 形态学梯度 + 轮廓长宽比筛选，不做 dnn；
- OCR 引擎只在独立 `ocr_worker.py` 进程内懒加载（`.[ocr]` optional extra），
  主进程不 import rapidocr/pytesseract；
- 缓存键：`source_hash + frame_sha256 + worker_version + policy_version`；
- 结果按时间对齐章节，注入 `ChapterVisualNote.screen_text` 与 Planner
  `visual_notes.screen_text`；只作候选事实，绝不写 IR；
- 引擎缺失/超时/取消时返回 warning，不阻断视觉摘要与规划。

## 主体跟踪 / 运动拆分 / 镜头聚类（P1）

- `perception/faces.py` 使用 centroid+IoU 关联输出 `track_id` 与出现区间，
  同一 track 做 EMA 平滑，平滑后的锚点供 9:16 裁切使用；
- `perception/view/flow.py` 用 RANSAC 仿射把光流拆成 `camera_mag` 与
  `subject_mag`，并给出 `camera_motion`（static/pan/tilt/zoom/camera）；
- `perception/cluster.py` 用 HSV 直方图相关性聚类镜头，章节只在视觉簇边界
  处切分，避免同一页 PPT 被拆成多章；
- 以上结果都只作候选/测量事实，不写时间线。

## 统一基础指纹（P1）

- `perception/view/fingerprint.py` 是 dHash/直方图的唯一实现；
  cv2 优先，缺失时回退 Pillow；
- `FINGERPRINT_VERSION=dhash:v2` 与 engine 写入 `MeasureResult.constants`
  及证据/质量/候选缓存键；`semantic_hash` 前缀带版本，CPU/GL 比对仍使用
  同一容差测试。

## 批量渲染与独立测量器（P2）

- `EvidenceRenderer.render_many(measure, requests)`：同一 measure 一次 context、
  共享解码图片缓存，循环输出多个网格/kind 的 `view.png`；CPU/GL 均实现，
  `ViewSynthesizer.synthesize_many` 逐请求生成独立 evidence/哈希。该入口只
  适用于同一 `MeasureResult` 的多请求；`build_visual_evidence_set` 的每种
  kind 有独立测量结果，仍逐视图 `render()`，共享的是候选窗口与抽帧。
- GL 仍只做拼版/编号/bbox；**不**在 shader 中计算光流、帧差或直方图。
- `perception/view/measure_gpu.py` 的 GpuMeasurer 与 renderer 解耦，只输出
  数值 `MeasureResult`；CPU 参考路径为 `measure_tiles`，CPU/GPU 用
  `compare_measure_results` 做数值容差比对；失败回退 CPU 并记录
  `measure_backend`（不参与 `render_hash`）。
- 只有 `accelerated=True` 的测量后端才可把候选窗口采样提升到
  `SMARTCUT_GPU_MEASURE_FPS`（≤30fps）；CPU 与 `CudaViewMeasurerPlaceholder`
  保持 `SMARTCUT_VIEW_FPS`。
- `QOpenGLWidget` 预览/叠层仅改善人审，优先级最低，不进入本轮实现。

## 可选本地视觉后端

- `SMARTCUT_VISION_PROVIDER=local` 时使用 `LocalVisionClient`，不发送图片到远程。
- 具体推理由外部模块注入：`SMARTCUT_LOCAL_VISION_MODULE` 指向一个可导入
  模块，需提供：
  `analyze(messages, schema, *, model_path=None) -> dict | BaseModel`。
- `SMARTCUT_LOCAL_VISION_BACKEND=onnx` 时额外要求 `onnxruntime` 可用且
  `SMARTCUT_LOCAL_VISION_MODEL_PATH` 存在；模型加载完全由插件负责。
- 模块缺失、依赖缺失或模型路径无效时 `available=False`，规划自动 text-only，
  不阻断渲染。
