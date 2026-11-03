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
```

- 未配置 base_url/model 时完全禁用，不影响文本规划，也不产生 warning。
- 若 base_url 与 DeepSeek 相同，可复用 DeepSeek Key；否则必须单独提供
  `VISION_API_KEY`，避免把 DeepSeek Key 发给第三方。
- 桌面设置可写入视觉 Base URL/模型，Key 存 Windows Credential Manager。

## 输入选择

1. 全局 contact sheet 最多 1 张（来自 `cache/contactsheet/*.jpg`）。
2. 选中章节各最多 2 张 keyframes（`ChapterPlan.keyframes`）。
3. 总数 ≤7；按 sha256 去重；长边缩放到 ≤1024，统一转 JPEG q82。
4. 单图/总字节上限遵循 `policy/vision.json` 与 Settings。
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

## 缓存与降级

- 缓存键：source_hash + 模型 + base_url + prompt_version + goal + 章节 id + 图片 sha256。
- 命中缓存不重复请求 VLM。
- 超时/HTTP 错误/JSON 失败/Pillow 缺失/图片缺失 → 返回 warning，使用纯文本规划。
- 视觉调用使用独立 `VisionClient`，与 DeepSeek 文本客户端共享 HTTP/缓存/资源池模式。

## 隐私

- 只有配置了 VLM 才会发送图片；默认关闭。
- 发送的是缩放后的 JPEG，不包含源文件路径。
- 日志不记录 base64 图片或 API Key。

## 可选本地视觉后端

- `SMARTCUT_VISION_PROVIDER=local` 时使用 `LocalVisionClient`，不发送图片到远程。
- 具体推理由外部模块注入：`SMARTCUT_LOCAL_VISION_MODULE` 指向一个可导入
  模块，需提供：
  `analyze(messages, schema, *, model_path=None) -> dict | BaseModel`。
- `SMARTCUT_LOCAL_VISION_BACKEND=onnx` 时额外要求 `onnxruntime` 可用且
  `SMARTCUT_LOCAL_VISION_MODEL_PATH` 存在；模型加载完全由插件负责。
- 模块缺失、依赖缺失或模型路径无效时 `available=False`，规划自动 text-only，
  不阻断渲染。
