"""视觉上下文：挑选 contact sheet/keyframes、缩放编码并构造 VLM 消息。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smartcut.config import Settings
from smartcut.contracts.time import seconds_from_mapping
from smartcut.contracts.vision import ImageVisualNotes, VisualNotes
from smartcut.contracts.workspace import Workspace
from smartcut.logging import get_logger

_LOG = get_logger("smartcut.planner.vision")

VISION_SYSTEM_PROMPT = """你是视频视觉分析器。只输出一个 JSON 对象，不要解释或 Markdown。
你会收到一张全局 contact sheet 和若干章节关键帧，并附带章节时间信息。
请为每个给定章节输出：
- chapter_id：必须与输入一致；
- summary：1-2 句画面内容/人物/场景描述；
- visual_tags：最多 5 个短标签；
- cautions：最多 3 条画面风险（模糊、遮挡、无主体等）。
另给出 overall 总体视觉摘要。
禁止输出文件路径、像素坐标、ffmpeg 命令或时间线操作。
输出必须符合以下 Schema：
"""


@dataclass(frozen=True)
class VisionImage:
    label: str
    mime: str
    data_url: str
    sha256: str
    size: int
    chapter_id: str | None = None


def _encode_image(
    workspace: Workspace,
    relative: str,
    label: str,
    settings: Settings,
    *,
    chapter_id: str | None = None,
) -> VisionImage | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        path = workspace.resolve(relative)
    except Exception as exc:
        _LOG.debug("vision_image_path_rejected", relative=relative, error=str(exc))
        return None
    if not path.is_file():
        return None
    try:
        with Image.open(path) as image:
            return _encode_pil_image(
                image,
                label,
                settings,
                chapter_id=chapter_id,
            )
    except Exception as exc:
        _LOG.debug("vision_image_open_failed", relative=relative, error=str(exc))
        return None


def _encode_pil_image(
    image,
    label: str,
    settings: Settings,
    *,
    chapter_id: str | None = None,
) -> VisionImage | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        rendered = image.convert("RGB")
        rendered.thumbnail(
            (settings.vision_image_max_edge, settings.vision_image_max_edge),
            Image.Resampling.LANCZOS,
        )
        buffer = io.BytesIO()
        rendered.save(buffer, format="JPEG", quality=82, optimize=True)
    except Exception as exc:
        _LOG.debug("vision_image_encode_failed", label=label, error=str(exc))
        return None
    payload = buffer.getvalue()
    if not payload or len(payload) > settings.vision_image_max_bytes:
        return None
    digest = hashlib.sha256(payload).hexdigest()
    return VisionImage(
        label=label,
        mime="image/jpeg",
        data_url="data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii"),
        sha256=digest,
        size=len(payload),
        chapter_id=chapter_id,
    )


def build_chapter_tile(
    workspace: Workspace,
    chapter_id: str,
    keyframes: list[str],
    settings: Settings,
    *,
    max_frames: int = 4,
) -> VisionImage | None:
    """把同章节最多 4 张关键帧拼成 2x2 tile，降低 VLM 图片数。"""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    paths = [str(item) for item in keyframes[:max_frames]]
    images = []
    for relative in paths:
        try:
            path = workspace.resolve(relative)
        except Exception:
            continue
        if not path.is_file():
            continue
        try:
            with Image.open(path) as image:
                images.append(image.convert("RGB").copy())
        except Exception:
            continue
    if not images:
        return None
    if len(images) == 1:
        return _encode_pil_image(
            images[0],
            f"章节 {chapter_id} 关键帧",
            settings,
            chapter_id=chapter_id,
        )
    columns = 2
    rows = (len(images) + columns - 1) // columns
    cell = max(64, settings.vision_image_max_edge // columns)
    canvas = Image.new("RGB", (columns * cell, rows * cell), "#111827")
    for index, image in enumerate(images):
        tile = ImageOps.fit(image, (cell, cell), method=Image.Resampling.LANCZOS)
        canvas.paste(tile, ((index % columns) * cell, (index // columns) * cell))
    return _encode_pil_image(
        canvas,
        f"章节 {chapter_id} 关键帧拼图",
        settings,
        chapter_id=chapter_id,
    )


def chapters_needing_vision(
    chapters: list[dict[str, Any]],
    *,
    min_chars: int = 40,
) -> list[dict[str, Any]]:
    """文本优先：摘要足够长的章节不再发送关键帧。"""
    result: list[dict[str, Any]] = []
    for chapter in chapters:
        summary = str(chapter.get("summary") or "").strip()
        if len(summary) < max(0, min_chars):
            result.append(chapter)
    return result


def select_images(
    workspace: Workspace,
    perception: dict | None,
    chapters: list[dict[str, Any]],
    settings: Settings,
) -> list[VisionImage]:
    images: list[VisionImage] = []
    seen: set[str] = set()
    total = 0

    def add(item: VisionImage | None) -> bool:
        nonlocal total
        if item is None or item.sha256 in seen:
            return True
        if len(images) >= settings.vision_max_images:
            return False
        if total + item.size > settings.vision_total_max_bytes:
            return False
        seen.add(item.sha256)
        images.append(item)
        total += item.size
        return True

    contact = (perception or {}).get("contact_sheet")
    if contact:
        add(_encode_image(workspace, contact, "全局 contact sheet", settings))

    for chapter in chapters:
        chapter_id = str(chapter.get("id") or "")
        if not chapter_id:
            continue
        keyframes = chapter.get("keyframes") or []
        if settings.vision_chapter_tiles and len(keyframes) > 1:
            if not add(
                build_chapter_tile(
                    workspace,
                    chapter_id,
                    [str(item) for item in keyframes],
                    settings,
                )
            ):
                return images
            continue
        limit = settings.vision_max_keyframes_per_chapter
        for relative in keyframes[:limit]:
            if len(images) >= settings.vision_max_images:
                return images
            if not add(
                _encode_image(
                    workspace,
                    str(relative),
                    f"章节 {chapter_id} 关键帧",
                    settings,
                    chapter_id=chapter_id,
                )
            ):
                return images
    return images


IMAGE_NOTES_SYSTEM_PROMPT = """你是视频关键帧描述器。只输出一个 JSON 对象，不要解释或 Markdown。
输入中的每张图片都带有 image_index 和章节信息。请为每张图片输出：
- image_index：必须与输入一致；
- summary：1-2 句画面内容/人物/场景描述；
- visual_tags：最多 5 个短标签；
- cautions：最多 3 条画面风险（模糊、遮挡、无主体等）。
不要输出文件路径、像素坐标、ffmpeg 命令或时间线操作。
输出必须符合以下 Schema：
"""


def build_image_note_messages(
    goal: str,
    chapters: list[dict[str, Any]],
    images: list[VisionImage],
) -> list[dict[str, Any]]:
    schema = json.dumps(
        ImageVisualNotes.model_json_schema(by_alias=True),
        ensure_ascii=False,
    )
    chapter_map = {str(item.get("id")): item for item in chapters}
    image_meta = []
    for index, image in enumerate(images):
        chapter = chapter_map.get(str(image.chapter_id or ""), {})
        image_meta.append(
            {
                "image_index": index,
                "chapter_id": image.chapter_id,
                "chapter_title": chapter.get("title"),
                "label": image.label,
            }
        )
    user_content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"目标：{goal}\n"
                f"图片元数据：{json.dumps(image_meta, ensure_ascii=False)}"
            ),
        }
    ]
    for index, image in enumerate(images):
        user_content.append(
            {"type": "text", "text": f"image_index={index}；{image.label}"}
        )
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": image.data_url, "detail": "low"},
            }
        )
    return [
        {"role": "system", "content": IMAGE_NOTES_SYSTEM_PROMPT + schema},
        {"role": "user", "content": user_content},
    ]


def build_vision_messages(
    goal: str,
    chapters: list[dict[str, Any]],
    images: list[VisionImage],
) -> list[dict[str, Any]]:
    schema = json.dumps(VisualNotes.model_json_schema(by_alias=True), ensure_ascii=False)
    chapter_payload = [
        {
            "chapter_id": chapter.get("id"),
            "title": chapter.get("title"),
            "start_seconds": seconds_from_mapping(chapter.get("start")),
            "end_seconds": seconds_from_mapping(chapter.get("end")),
        }
        for chapter in chapters
    ]
    user_content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"目标：{goal}\n"
                f"章节：{json.dumps(chapter_payload, ensure_ascii=False)}"
            ),
        }
    ]
    for image in images:
        user_content.append({"type": "text", "text": image.label})
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": image.data_url, "detail": "low"},
            }
        )
    return [
        {"role": "system", "content": VISION_SYSTEM_PROMPT + schema},
        {"role": "user", "content": user_content},
    ]


