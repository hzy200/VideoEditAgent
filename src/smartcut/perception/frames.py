"""抽帧清单读写与自适应选帧。"""

from __future__ import annotations

import bisect
import json
import os
from pathlib import Path
from uuid import uuid4

from smartcut.contracts.perception import FrameRef
from smartcut.contracts.time import RationalTime


def list_frames(
    directory: Path,
    workspace_root: Path,
    fps: float,
    thumbnail_dir: Path | None = None,
) -> list[FrameRef]:
    paths = sorted(directory.glob("frame_*.jpg"))
    thumbnails = (
        sorted(thumbnail_dir.glob("thumb_*.jpg")) if thumbnail_dir is not None else []
    )
    frames: list[FrameRef] = []
    for index, path in enumerate(paths):
        thumbnail = None
        if index < len(thumbnails):
            thumbnail = thumbnails[index].relative_to(workspace_root).as_posix()
        frames.append(
            FrameRef(
                index=index,
                t=RationalTime.from_seconds(index / fps, rate=90_000),
                path=path.relative_to(workspace_root).as_posix(),
                thumbnail=thumbnail,
            )
        )
    return frames


def write_manifest(path: Path, frames: list[FrameRef]) -> None:
    """先写临时文件再原子替换，避免取消/崩溃留下半截 manifest。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.part-{uuid4().hex[:8]}")
    payload = json.dumps(
        [frame.model_dump(mode="json") for frame in frames],
        ensure_ascii=False,
        indent=2,
    )
    try:
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def read_manifest(path: Path) -> list[FrameRef]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [FrameRef.model_validate(item) for item in payload]


def select_analysis_frames(
    frames: list[FrameRef],
    scene_times: list[float] | None = None,
    *,
    max_frames: int = 0,
    risk_times: list[float] | None = None,
    topic_times: list[float] | None = None,
    chapter_bounds: list[float] | None = None,
    min_gap_seconds: float = 0.5,
    dedupe: bool = False,
    workspace_root: Path | None = None,
    hash_distance: int = 4,
) -> list[FrameRef]:
    """按预算和优先级选帧。

    优先级：硬切点 > 质量风险 > 话题变化 > 章节首尾 > 均匀覆盖。
    ``dedupe`` 开启时用感知哈希跳过相似帧；只有不高于当前候选优先级
    的已选帧会触发去重，保证风险/切点帧优先保留。
    """
    if max_frames <= 0:
        return frames
    if not frames:
        return []
    if len(frames) <= max_frames and not dedupe:
        return frames

    times = [frame.t.to_float() for frame in frames]
    picked: dict[int, int] = {}
    hashes: dict[int, int | None] = {}

    def perceptual_hash(index: int) -> int | None:
        if index not in hashes:
            path = workspace_root / frames[index].path if workspace_root else None
            hashes[index] = dhash(path) if path is not None else None
        return hashes[index]

    def too_similar(index: int, priority: int) -> bool:
        if not dedupe or workspace_root is None:
            return False
        value = perceptual_hash(index)
        if value is None:
            return False
        for other, other_priority in picked.items():
            if other_priority > priority:
                continue
            other_value = perceptual_hash(other)
            if other_value is None:
                continue
            if _hamming(value, other_value) <= hash_distance:
                return True
        return False

    def can_add(index: int, priority: int) -> bool:
        if index in picked:
            return False
        moment = times[index]
        if any(abs(moment - times[other]) < min_gap_seconds for other in picked):
            return False
        return not too_similar(index, priority)

    def add_nearest(value: float, priority: int, *, both_sides: bool = False) -> None:
        position = bisect.bisect_left(times, float(value))
        options: list[int] = []
        if both_sides:
            if position - 1 >= 0:
                options.append(position - 1)
            if position < len(frames):
                options.append(position)
        else:
            for index in (position - 1, position):
                if 0 <= index < len(frames):
                    options.append(index)
            options.sort(key=lambda index: (abs(times[index] - value), index))
        for index in options:
            if len(picked) >= max_frames:
                return
            if can_add(index, priority):
                picked[index] = priority
                if not both_sides:
                    return

    for values, priority, both in (
        (scene_times, 0, True),
        (risk_times, 1, False),
        (topic_times, 2, False),
        (chapter_bounds, 3, False),
    ):
        for value in values or []:
            if len(picked) >= max_frames:
                break
            add_nearest(float(value), priority, both_sides=both)

    remaining = max_frames - len(picked)
    if remaining > 0:
        candidates = [index for index in range(len(frames)) if index not in picked]
        step = max(1, len(candidates) / remaining)
        cursor = 0.0
        while remaining > 0 and cursor < len(candidates):
            index = candidates[min(len(candidates) - 1, int(cursor))]
            if can_add(index, 4):
                picked[index] = 4
                remaining -= 1
            cursor += step
        for index in candidates:
            if remaining <= 0:
                break
            if can_add(index, 4):
                picked[index] = 4
                remaining -= 1

    return [frames[index] for index in sorted(picked)]


def dhash(path: Path) -> int | None:
    """统一指纹实现；缺少 Pillow/OpenCV 或读图失败时返回 None。"""
    try:
        from PIL import Image

        from smartcut.perception.view import fingerprint
    except ImportError:  # pragma: no cover - vision 是 optional extra
        return None
    try:
        with Image.open(path) as image:
            return fingerprint.dhash(image)
    except Exception:
        return None


def _hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()
