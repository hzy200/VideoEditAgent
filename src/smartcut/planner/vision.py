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
    evidence_id: str | None = None
    tile_metadata: tuple[dict[str, Any], ...] = ()


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


RISK_KINDS = {"black", "frozen", "blurry", "duplicate"}
RISK_TOLERANCE_SECONDS = 0.5


def _quality_marks(perception: dict | None) -> list[tuple[float, str]]:
    marks = ((perception or {}).get("quality") or {}).get("marks") or []
    result: list[tuple[float, str]] = []
    for mark in marks:
        kind = str(mark.get("kind") or "")
        if kind not in RISK_KINDS:
            continue
        seconds = seconds_from_mapping(mark.get("t"))
        if seconds is not None:
            result.append((float(seconds), kind))
    return result


def _risk_at(
    moment: float | None,
    marks: list[tuple[float, str]],
) -> str | None:
    if moment is None or not marks:
        return None
    nearest = min(marks, key=lambda item: abs(item[0] - moment))
    if abs(nearest[0] - moment) > RISK_TOLERANCE_SECONDS:
        return None
    return nearest[1]


def _measurement_facts(measurement: dict) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    d_hist = measurement.get("d_hist")
    if d_hist is not None:
        facts["d_hist"] = round(float(d_hist), 2)
    flow_mag = measurement.get("flow_mag")
    if flow_mag is not None:
        facts["flow_mag"] = round(float(flow_mag), 2)
    flow_dir = measurement.get("flow_dir_deg")
    if flow_dir is not None:
        facts["flow_dir_deg"] = round(float(flow_dir))
    score = measurement.get("score")
    if score is not None:
        facts["score"] = round(float(score), 2)
    camera_mag = measurement.get("camera_mag")
    if camera_mag is not None:
        facts["camera_mag"] = round(float(camera_mag), 2)
    subject_mag = measurement.get("subject_mag")
    if subject_mag is not None:
        facts["subject_mag"] = round(float(subject_mag), 2)
    camera_motion = measurement.get("camera_motion")
    if camera_motion:
        facts["camera_motion"] = str(camera_motion)
    bbox = measurement.get("bbox_xywh")
    width = float(measurement.get("width") or 0)
    height = float(measurement.get("height") or 0)
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4 and width > 0 and height > 0:
        area = float(bbox[2]) * float(bbox[3])
        facts["face"] = round(min(1.0, area / (width * height)), 2)
    return facts


def _evidence_tile_metadata(
    payload: dict,
    quality_marks: list[tuple[float, str]] | None = None,
) -> tuple[dict[str, Any], ...]:
    tiles = ((payload.get("tile_map") or {}).get("tiles")) or []
    measurements = payload.get("measurements") or []
    marks = quality_marks or []
    metadata: list[dict[str, Any]] = []
    for index, tile in enumerate(tiles):
        seconds = seconds_from_mapping(tile.get("t"))
        entry: dict[str, Any] = {
            "label": str(tile.get("label") or ""),
            "t_seconds": seconds,
            "pts": tile.get("pts"),
            "frame_no": tile.get("frame_no"),
        }
        if index < len(measurements) and isinstance(measurements[index], dict):
            facts = _measurement_facts(measurements[index])
            # measure_tiles 把首帧 d_hist 置为 1.0 作为占位；不要当作真实变化。
            if index == 0 and facts.get("d_hist") == 1.0:
                facts.pop("d_hist", None)
            entry.update(facts)
        risk = _risk_at(
            float(seconds) if seconds is not None else None,
            marks,
        )
        if risk is not None:
            entry["risk"] = risk
        metadata.append(entry)
    return tuple(metadata)


def select_evidence_images(
    workspace: Workspace,
    evidence: dict | list[dict] | tuple[dict, ...] | None,
    settings: Settings,
    *,
    perception: dict | None = None,
) -> list[VisionImage]:
    """把已被 top-K 预算筛选过的证据视图编码为 VLM 图片。

    接受单个视图（旧调用）或视图列表。多视图必须由服务层预先分配不相交的
    Txx 标签区间；这里只负责按全局图片/字节预算编码与去重。
    """
    if isinstance(evidence, (list, tuple)):
        views = [item for item in evidence if isinstance(item, dict)]
    else:
        views = [evidence] if isinstance(evidence, dict) else []
    quality_marks = _quality_marks(perception)
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
        total += item.size
        images.append(item)
        return True

    for view in views:
        payload = view.get("evidence") or {}
        image_ref = str(view.get("image_ref") or payload.get("image_ref") or "")
        if not payload or not image_ref:
            continue
        kind = str(payload.get("kind") or view.get("kind") or "contact_sheet")
        metadata = _evidence_tile_metadata(payload, quality_marks)
        labels = [item["label"] for item in metadata if item["label"]]
        label_text = (
            f"（{labels[0]}–{labels[-1]}）"
            if len(labels) > 1
            else f"（{labels[0]}）" if labels else ""
        )
        encoded = _encode_image(
            workspace,
            image_ref,
            f"视觉证据视图[{kind}]{label_text}",
            settings,
        )
        if encoded is None:
            continue
        if not add(
            VisionImage(
                label=encoded.label,
                mime=encoded.mime,
                data_url=encoded.data_url,
                sha256=encoded.sha256,
                size=encoded.size,
                evidence_id=str(payload.get("id") or ""),
                tile_metadata=metadata,
            )
        ):
            break
        for extra in view.get("extra_images") or []:
            if not add(
                _encode_image(
                    workspace,
                    str(extra),
                    f"视觉证据补充视图[{kind}]",
                    settings,
                )
            ):
                return images
    return images


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
- cautions：最多 3 条画面风险（模糊、遮挡、无主体等）；
- evidence_refs：仅当图片带 evidence_id/tiles 时输出，最多 5 个与描述最相关的
  Txx 标签；只能引用输入中真实存在的标签，引用不存在的标签视为错误。
证据时间线给出 [Txx|秒|测量...] 与相邻语音文本，只用于理解画面与语音关系。
测量字段含义：d_hist=相邻帧直方图距离(0-1)，flow=光流强度，dir=光流方向角度，
face=人脸框面积占比(0-1)，score=测量综合分(0-1)，risk=该时刻质量标记。
cam/subj/motion 分别表示全局相机运动、残差主体运动和相机运动类型。
这些是数值测量，不是语义结论；缺失字段表示未测量，不得臆造。flow/dir/cam/
subj/motion 只在 temporal_diff/flow_strip 视图中出现，contact_sheet 没有运动
测量时不得编造。
不得输出时间算术、切点建议、文件路径、像素坐标或 ffmpeg 命令。
输出必须符合以下 Schema：
"""


def _tile_measurement_suffix(tile: dict[str, Any]) -> str:
    """把测量值压成一行紧凑事实；缺失字段不输出，避免模型臆造。"""
    parts: list[str] = []
    if tile.get("d_hist") is not None:
        parts.append(f"d_hist {float(tile['d_hist']):.2f}")
    if tile.get("flow_mag") is not None:
        parts.append(f"flow {float(tile['flow_mag']):.1f}")
    if tile.get("flow_dir_deg") is not None:
        parts.append(f"dir {float(tile['flow_dir_deg']):.0f}")
    if tile.get("face") is not None:
        parts.append(f"face {float(tile['face']):.2f}")
    if tile.get("score") is not None:
        parts.append(f"score {float(tile['score']):.2f}")
    if tile.get("camera_mag") is not None:
        parts.append(f"cam {float(tile['camera_mag']):.1f}")
    if tile.get("subject_mag") is not None:
        parts.append(f"subj {float(tile['subject_mag']):.1f}")
    if tile.get("camera_motion"):
        parts.append(f"motion {tile['camera_motion']}")
    if tile.get("risk"):
        parts.append(f"risk {tile['risk']}")
    return "".join(f"|{part}" for part in parts)


def _evidence_timeline(
    images: list[VisionImage],
    transcript: Any | None,
) -> str:
    entries: list[tuple[str, float, str, dict[str, Any]]] = []
    seen: set[str] = set()
    segments = list(getattr(transcript, "segments", []) or []) if transcript else []
    for image in images:
        for tile in image.tile_metadata:
            label = str(tile.get("label") or "")
            seconds = tile.get("t_seconds")
            if not label or label in seen or seconds is None:
                continue
            seen.add(label)
            text = ""
            for segment in segments:
                start = segment.start.to_float()
                end = segment.end.to_float()
                if start <= float(seconds) < end:
                    text = segment.text.strip()
                    break
            if not text and segments:
                nearest = min(
                    segments,
                    key=lambda segment: abs(segment.start.to_float() - float(seconds)),
                )
                if abs(nearest.start.to_float() - float(seconds)) <= 2.0:
                    text = nearest.text.strip()
            entries.append((label, float(seconds), text, tile))
    entries.sort(key=lambda item: (item[1], item[0]))
    lines = []
    for label, seconds, text, tile in entries[:64]:
        speech = f"[语音] {text[:80]}" if text else "[无语音]"
        suffix = _tile_measurement_suffix(tile)
        lines.append(f"[{label}|{seconds:.3f}s{suffix}] {speech}")
    return "\n".join(lines)


def build_image_note_messages(
    goal: str,
    chapters: list[dict[str, Any]],
    images: list[VisionImage],
    *,
    transcript: Any | None = None,
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
                "evidence_id": image.evidence_id,
                # 测量值只在时间线里出现一次，避免 prompt 重复膨胀。
                "tiles": [
                    {
                        "label": tile.get("label"),
                        "t_seconds": tile.get("t_seconds"),
                        "pts": tile.get("pts"),
                        "frame_no": tile.get("frame_no"),
                    }
                    for tile in image.tile_metadata
                ],
            }
        )
    timeline = _evidence_timeline(images, transcript)
    header = f"目标：{goal}\n图片元数据：{json.dumps(image_meta, ensure_ascii=False)}"
    if timeline:
        header += f"\n证据时间线：\n{timeline}"
    user_content: list[dict[str, Any]] = [
        {"type": "text", "text": header}
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


