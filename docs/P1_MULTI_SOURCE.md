# P1 多源时间线设计（完整多轨）

- 状态：设计草案（本轮不实现）
- 目标：支持多素材、多条视频轨、叠加/转场、多轨音频，接近 NLE 时间线
- 约束：不得把转码 profile 塞进 `EditIR`；模型仍只能输出结构化编辑决策

## 1. 现状与差距

当前 `EditIR` 是单源模型：

- `EditIR.media_facts` 只有一个 `MediaFacts`；
- `TrimOp.src` 必须等于 `media_facts.src`；
- 项目只有一个主 `MediaAsset`（`ingest` 覆盖最新素材）；
- Compiler 只生成 `cut → concat → postprocess → mix_audio → verify`；
- Runner 的视频工具只映射 `0:v:0`。

要支持完整多轨，必须同时改造契约、数据库、Compiler、Runner、Services 与 GUI。

## 2. 契约草案（v2）

建议新增 `schema = "editir/v2"`，保留 v1 解析能力。

```text
EditIRv2
  project_id
  revision
  schema = "editir/v2"
  assets: list[MediaAssetRef]        # 项目内素材引用，不重复 facts
  video_tracks: list[VideoTrack]
  audio: AudioSpec                   # 复用现有 audio.tracks
  subtitles
  export
  effects

MediaAssetRef
  id: str                            # a_xxx
  src: str                           # workspace 相对路径
  facts: MediaFacts                  # 或仅 asset_id，由 Services 解析
  source_hash: str

VideoTrack
  id: str
  clips: list[VideoClip]
  opacity: 0..1
  enabled: bool

VideoClip
  id: str
  asset_id: str
  source_range: TimeRange            # 源时间
  timeline_start: RationalTime       # 输出时间
  transform: {x,y,scale,rotation}?   # 归一化，禁止像素坐标
  effects: list[EffectSpec]
  transition_in / transition_out: TransitionSpec?

TransitionSpec
  type: Literal["crossfade","fade","wipe"]
  duration: RationalTime
  # 只允许 Compiler 转成 xfade/overlay，模型不输出 filter
```

设计原则：

1. 时间仍全部使用 `RationalTime`，输出时间由 Compiler 推导。
2. `assets` 只引用工作区文件；模型不得输出绝对路径。
3. 视频轨顺序 = 合成顺序；上层轨覆盖下层。
4. 转场属于时间轴词汇，不能放进 `effects`。
5. 音频继续复用现有 `AudioTrackSpec`，新增 `ducking` 已落地。

## 3. 数据库迁移

`media_assets` 当前每项目只有一条“主素材”，需要改为真正的多素材表：

- 增加 `kind`：`video` / `audio` / `image` / `render`；
- 增加 `is_primary`（或 `role`）：主素材、BGM、贴片；
- 保留 `source_hash`、`facts_json`；
- `ProjectAudioAsset` 可合并进 `media_assets`，或保留并增加 `asset_id` 关联；
- 迁移只增不删：旧项目把现有主素材标记为 `is_primary=1, kind=video`；
- 新增 `project_asset_refs` 不必要，直接用 `media_assets`。

API 需要新增：

```text
POST   /projects/{id}/assets           # 导入第二份素材
GET    /projects/{id}/assets
DELETE /projects/{id}/assets/{asset_id}
```

## 4. Compiler 改造

新增 `compiler/lower_v2.py`，仍保持纯函数：

1. 解析所有 `asset_id` → `MediaAssetRef`；
2. 每个 clip 生成一个 `cut`（或 `trim`）ToolCall，输出到 run 独占目录；
3. 单视频轨：
   - 相邻 clip 无转场 → `concat` demuxer；
   - 有转场 → 生成 `xfade`/`overlay` 的 `compose` ToolCall；
4. 多视频轨：
   - 先按轨拼接，再用 `overlay`/`blend` 合成为主视频；
   - 音频由 `mix_audio` 统一处理；
5. 最后仍是单次 `postprocess`（fit/字幕/响度）+ `verify`。

新增 Runner 工具：

- `compose`：输入多个视频/图片与结构化布局，输出合成视频；
- `xfade`：两段之间的转场；
- `overlay`：轨道叠加（支持透明度/时间区间）。

所有工具仍只接受工作区相对路径与结构化参数，禁止模型输出 filter。

## 5. Runner 与执行约束

- `cut` 片段必须使用统一编码参数，才能 concat；转场前必须重编码。
- 多轨合成必须一次 `filter_complex` 完成，避免多次全片重编码。
- 每个输入先 probe；分辨率/帧率/色彩不一致时：
  - 默认统一到项目输出 profile；
  - 记录 warning，不阻断；
  - HDR/10bit 需显式 tonemap 或保留策略。
- Segment 缓存键需包含：`asset_hash + source_range + 编码参数 + 轨道布局/转场参数`，且不含 `dst`。
- 多轨并行的 `ffmpeg_slot` 仍由 `ResourceManager` 统一限制。

## 6. Services 与 GUI

Services：

- `ingest` 不再覆盖主素材，新增 `add_asset` / `list_assets` / `remove_asset`；
- `get_media_facts` 改为按 asset_id 取；
- `plan` 上下文注入 `assets` 摘要（id、时长、分辨率、音轨）；
- `dry_run/render` 支持 v2 IR；
- 项目 bundle 并行加载 asset 列表。

GUI：

- 左侧“素材箱”：多素材导入、拖入时间线；
- 时间线：视频轨/音频轨分层，clip 可拖拽/裁剪/分割；
- 转场：相邻 clip 之间插入，时长可编辑；
- 预览：多轨合成代理（可先只预览主轨）；
- 未保存确认、Undo/Redo 仍走 Controller 快照。

## 7. 分阶段实施

### M1：多输入单轨拼接（低风险）

- 新增多 asset 表与导入 API；
- `EditIR` 仍 v1，但允许 `sequence` 引用不同 `src`（需先放开 `_single_source`）；
- Compiler 对每个 src 生成 cut，concat 前统一编码参数；
- 不做转场/多轨；
- 验收：两个不同素材顺序拼接，时长/音画同步正确。

### M2：单视频轨 + 转场

- 引入 v2 契约与 `compose/xfade`；
- 相邻 clip 的 `xfade`；
- 转场期间音频交叉淡化；
- 验收：两种转场、不同分辨率/帧率素材。

### M3：多视频轨叠加

- `overlay`/`blend`，透明度、时间区间；
- 画中画/贴纸；
- 验收：主轨 + 上层轨叠加，缓存可复用。

### M4：GUI 时间线

- 素材箱、多轨拖拽、转场手柄；
- 预览代理与最终渲染一致；
- 验收：完整编辑-撤销-保存-渲染闭环。

## 8. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 时间基/帧率不一致 | 统一 CFR/输出 profile；显式 warning |
| 色彩/HDR 不一致 | 项目级色彩策略；tonemap 或保留 |
| AAC 转场处音画漂移 | 音频统一重编码 + 转场交叉淡化 + A/V sync 校验 |
| 多轨性能 | 一次 filter_complex；Segment/合成缓存；ResourceManager |
| 旧项目兼容 | v1 IR 继续可读；迁移只增不删；渲染 v1 走旧 Lower |
| 模型越权 | 只输出 asset_id/时间/轨/转场类型；Compiler 生成 argv |

## 9. 明确不在本轮

- 实时多轨预览引擎；
- 关键帧动画曲线；
- 复杂调色/抠像；
- 多机分布式渲染；
- OTIO/FCPXML 交换（可后续基于 v2 契约映射）。
