# 媒体管线：编码、代理与回退

## 编码器策略

- 白名单：`libx264`、`h264_nvenc`、`h264_qsv`、`h264_amf`
  （见 `src/smartcut/policies/encoding.json`）。
- `SMARTCUT_HARDWARE_ENCODING=auto`（默认）：探测 ffmpeg 可用编码器，
  按 `SMARTCUT_ENCODER_PRIORITY` 选择；`off` 时直接用 libx264。
- 探测只在首次编译/代理时发生，通过 `ProcessRunner` 执行
  `ffmpeg -hide_banner -encoders`；结果按 ffmpeg 路径 + size/mtime 缓存。
- 默认 libx264 argv 与旧版本逐字节一致，golden 测试不受影响。

## 参数映射（纯函数）

见 `compiler/encoding.py`：

| codec | 参数 |
|---|---|
| libx264 | `-c:v libx264 -preset <preset> -crf <q>` |
| h264_nvenc | `-c:v h264_nvenc -preset <p> -rc vbr -cq <q> -b:v 0` |
| h264_qsv | `-c:v h264_qsv -preset <p> -global_quality <q>` |
| h264_amf | `-c:v h264_amf -quality <p> -rc cqp -qp_i/-qp_p/-qp_b <q>` |

`lower(ir, encoder=...)` 只把 profile 注入 cut/postprocess ToolCall；
模型永远接触不到这些参数。

## 单次后处理

- `fit` + 烧录字幕 + 全局音频调整 + `loudnorm` 合并为一个 `postprocess` ToolCall。
- 有视频 filter 时只重编码一次；只有音频 filter 时 `-c:v copy`，仅重编码音频。
- 全局音频链路顺序：`pitch → volume → loudnorm`。
  - 音量：`volume={db}dB`。
  - 音调保持时长：`aresample=48000,asetrate=48000*f,aresample=48000,
    atempo=1/f`，`f = 2 ** (semitones/12)`。
- 手动字幕由 `EditIR.subtitles` 提供（输出时间线），非空时不再依赖转写；
  空列表表示明确无字幕，`None` 表示自动生成。
- 旧的 fit/subtitles/loudnorm 工具仍保留用于手工/兼容，但 Compiler 默认不再串联它们。

## 多轨混音

- `EditIR.audio.tracks`：`None`=旧单音轨；`[]`=明确静音；非空=多轨。
- Compiler 解析 `AudioAssetRef`，按 `muted`/`solo` 过滤，生成一次
  `mix_audio` ToolCall；视频流 `-c:v copy`，不改画面。
- 每轨：`atrim → asetpts → volume → afade → adelay`；
  循环轨道在输入层加 `-stream_loop -1`，并把 `atrim` 铺满剩余时长；
  多轨 `amix normalize=0`，最后 `apad/atrim` 对齐成片时长。
- 预览混音使用同一 `mix_audio`，输出 128k AAC 到 `proxy/mix_<key>.mp4`；
  键包含轨道参数、主素材哈希与各音频资产哈希。
- 主输出 `volume/pitch/loudnorm` 在混音后统一应用。
- 无活动音轨时用 `anullsrc` 生成静音轨，保证成片音轨存在。
- 音轨超出成片时长时 Compiler 截断并记录 warning。

## 独立转码/封装（P1）

- `contracts/transcode.py` 定义与 `EditIR` 解耦的 `TranscodeRequest` / `TranscodeProfile`。
- 支持容器 `mp4/mov/mkv`；视频 `copy/libx264/libx265/libsvtav1/prores_ks`；
  音频 `copy/aac/ac3/libopus/flac/pcm`；字幕 `copy/mov_text/srt/ass/webvtt`。
- 支持分辨率/缩放方式、fps/fps_mode、pix_fmt、10bit、HDR tonemap、autorotate、
  faststart、多音轨、外挂/内嵌软字幕、元数据与章节元数据。
- 服务入口：`Services.transcode` + `POST /api/projects/{id}/transcode`；
  桌面“导出 / 转码…”对话框通过 `DesktopController.transcode` 提交 Job。
- 输出写入项目 `exports/`；缓存键 = 源哈希 + profile + 外挂字幕/音频哈希，命中直接复制。
- 两遍 loudnorm：先 `loudnorm_measure` 输出 JSON，再把 measured_* 注入第二遍。
- 渲染管线同样支持单音轨两遍 loudnorm（`render_loudnorm_two_pass`）。

## 输入规范化

- 渲染 cut/postprocess 默认 `-fps_mode cfr`（copy 模式保留源 VFR 并告警）。
- 源素材 10bit 时渲染输出 8bit `yuv420p`，Compiler 明确记录降级 warning。
- HDR 色彩传输默认只告警；`SMARTCUT_RENDER_HDR_TONEMAP=1` 时在 postprocess
  执行 `zscale + tonemap`。
- 独立转码支持 `hdr_tonemap`、`keep_10bit`、`autorotate` 的显式 profile 控制。

## 音频/字幕增强

- `AudioTrackSpec.ducking`：以主/人声轨为 sidechain，对 BGM 做
  `sidechaincompress`，并支持闪避增益/比例/阈值/攻击/释放。
- 字幕烧录支持 SRT/ASS；`caption_fonts_dir` 传入 `fontsdir`。
- 独立转码可把 SRT/ASS/VTT 作为软字幕封装进 mp4/mov/mkv。

## 人声分离（可选）

- `runner/separation.py` 定义 `Separator` 协议：
  `available()` + `separate(source, output_dir, model, stems, cancel_token, progress)`。
- 当前实现 `DemucsSeparator`（2-stem 默认，支持 4-stem 参数）；
  UVR/MDX 后续实现同一协议，由 `build_separator()` 工厂选择。
- 通过外部 Demucs 命令运行，主程序不导入 PyTorch。
- 打包形态：`SmartCut.exe` + `separator/SmartCut-Separator.exe`；
  worker 内含 torch/CUDA 与 `models/demucs`，由主程序以子进程调用。
- worker 通过 `SMARTCUT_DEMUCS_PROGRESS_FILE` 写 JSON 进度；
  `auto` 设备优先 CUDA，不可用时回退 CPU。
- 构建验证：CUDA 12.6 / torch 2.6.0+cu126 / RTX 4060；
  伴生 worker 约 4.6GB，完整分发目录约 5.6GB。
- 命令来源：`SMARTCUT_DEMUCS_COMMAND` → PATH `demucs` → Python 模块。
- 输入先抽成 44.1k 立体声 WAV；输出 vocals/no_vocals 映射为
  人声/伴奏，复制到 `cache/stems/<key>` 并注册为 `project_audio_assets`。
- 缓存键 = `source_hash + demucs_v1 + model`；命中不重复推理。
- 适配器使用 `ResourceManager.cpu_slot`，支持取消与超时；
  模型/权重许可需单独核对，商用分发前评估。

## 失败回退

1. 单个带 encoder 的工具失败 → 记录失败 codec，去掉 `encoder` 参数，
   用 libx264 重试当前调用。
2. 失败 codec 在本 Services 进程内不再选择。
3. verify 失败且本次使用过硬件编码 → 强制软件重渲一次；
   仍失败才向上抛错。
4. 结果包含 `actual_encoder`、`encoders_used`、fallback warnings。

## 特效

- `EditIR.effects` 支持 `fit/color/blur/sharpen/vignette/fade/zoom`，
  作用域为 `target=output`（全局）或 `target=segment`（逐片段）。
- 全局特效：Compiler 生成 `video_filters_pre`（color/blur/sharpen/vignette，
  在 fit 之前）与 `video_filters_post`（zoom/fade，在 fit 之后）。
- 逐片段特效：写入该片段 `cut` 的 `video_filters`；fade/zoom 以片段时长为基准；
  `fit` 只允许全局。逐片段滤镜要求 reencode，copy 模式明确拒绝。
- Runner 对两段滤镜都做白名单校验；`effects=[]` 时 argv 与旧版本逐字节一致。
- 桌面“预览特效”使用 `lower(preview_height=480)` 并跳过最终 verify，
  输出到 `proxy/effects_<key>/output.mp4`；键包含完整 IR 与预览高度。

## 转场 / 变速 / 水印

- `TrimOp.speed`（0.5–2.0）：`cut` 在输入层加 `setpts=PTS/speed` 与
  `atempo=speed`；输出时长 = 源区间 / speed。`copy` 模式拒绝变速。
- `TransitionSpec`：在 `after` 片段与下一片段之间做重叠；Compiler 切换为
  `compose` 工具，使用 `xfade` + `acrossfade` 的 `filter_complex`，
  无转场的间隔用 `concat` 滤镜。输出时长 = 片段时长和 - 重叠和。
- `OverlaySpec`：文字水印用 `drawtext`；图片/Logo 水印引用
  `project_overlay_assets` 的 `asset_id`，在 `postprocess` 用多输入 `overlay`
  合成。起止时间使用输出时间线，不改变时长。
- `WatermarkRemovalSpec`：GUI 手动标注矩形区域，`postprocess` 生成 `delogo`；
  坐标基于输出画面，模型不得输出像素坐标。ffmpeg 要求区域不接触画面边界。
- 字幕重映射：源词时间按 speed 缩放，并按转场重叠推进输出起点。
- 转场、变速、水印均要求 reencode；`copy` 模式明确拒绝。

## Segment 缓存

- 键：无滤镜 `hash(source_hash, "segment_v1", CutArgs 去掉 dst)`；
  逐片段特效使用 `segment_v2`，并把 `video_filters` 纳入键。
- 编码器参数属于 CutArgs，因此硬件/软件片段不会互相误用。
- 命中时直接复制缓存文件到目标路径，不重新起 ffmpeg。
- 并行 cut 线程数 = `min(SMARTCUT_FFMPEG_CONCURRENCY, cut 数)`；
  输出目录先在主线程创建，避免 Windows 路径解析竞态。

## 产物落盘与原子性

- 所有带 `dst` 的工具先写同目录临时文件，成功后 `os.replace` 到最终路径；
  取消/失败清理临时文件，避免半截视频/音频被缓存命中。
- 字幕 SRT/VTT、`frames/manifest.json`、checkpoint context digest、
  `result.json`、缓存 JSON/TXT 均走 `server/atomic.py` 的原子写。
- 抽帧写入唯一 `cache/frames/{key}.part-*` 目录，成功后整体替换最终目录；
  失败/取消删除临时目录。
- 硬件编码回退时最终 `ToolResult.encoder` 记为 `libx264`；copy 透传的
  `actual_encoder` 记为 `copy`，不再误报为 libx264。
- 生产转写使用独立 whisper worker 进程；崩溃/超时按
  `SMARTCUT_WHISPER_WORKER_RETRIES` 受控重启，主进程不加载 CTranslate2 模型。

## Proxy-first

- 项目打开时优先加载已有 proxy；不存在则在后台生成 720p 代理。
- 代理使用相同编码器策略，失败自动回退 libx264。
- 预览面板显示“代理预览/成片预览”；最终渲染完成后切换到成片。
- 感知（质量/人脸/镜头）仍读取源素材，不使用代理，避免降低检测精度。

## 校验

- 每个成片最后一步是独立 ffprobe `verify`：时长、帧率、音轨、分辨率。
- `--self-check` 会输出可用编码器与最终选择。
- 测试通过 `hardware_encoding=off` 保证确定性；编码器参数/回退有独立单测。
