"""人脸锚点：OpenCV Haar cascade（归一化 bbox），按帧并行检测。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

from smartcut.contracts.faces import FaceAnchor, FaceReport, FaceTrack
from smartcut.contracts.perception import FrameRef
from smartcut.errors import (
    PerceptionUnavailable,
    RunCancelledError,
    ToolExecutionError,
)
from smartcut.perception import process_pool

_THREAD_LOCAL = threading.local()
TRACKING_VERSION = "iou@1.0"


def _iou(left: FaceAnchor, right: FaceAnchor) -> float:
    x1 = max(left.x, right.x)
    y1 = max(left.y, right.y)
    x2 = min(left.x + left.w, right.x + right.w)
    y2 = min(left.y + left.h, right.y + right.h)
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = left.w * left.h + right.w * right.h - intersection
    return intersection / union if union > 0 else 0.0


def _center_gap(left: FaceAnchor, right: FaceAnchor) -> float:
    return max(
        abs(left.center_x - right.center_x),
        abs(left.center_y - right.center_y),
    )


def _clamp_box(x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
    w = max(0.001, min(1.0, w))
    h = max(0.001, min(1.0, h))
    x = max(0.0, min(1.0 - w, x))
    y = max(0.0, min(1.0 - h, y))
    return x, y, w, h


def derive_tracks(anchors: list[FaceAnchor]) -> list[FaceTrack]:
    groups: dict[int, list[FaceAnchor]] = {}
    for anchor in anchors:
        if anchor.track_id is None:
            continue
        groups.setdefault(anchor.track_id, []).append(anchor)
    tracks: list[FaceTrack] = []
    for track_id in sorted(groups):
        values = sorted(groups[track_id], key=lambda item: item.t.as_fraction())
        tracks.append(
            FaceTrack(
                track_id=track_id,
                start=values[0].t,
                end=values[-1].t,
                frame_count=len(values),
            )
        )
    return tracks


def track_anchors(
    anchors: list[FaceAnchor],
    *,
    iou_threshold: float = 0.3,
    max_gap_seconds: float = 2.0,
    smoothing_alpha: float = 0.4,
) -> tuple[list[FaceAnchor], list[FaceTrack]]:
    """centroid + IoU 关联，并对同一 track 做 EMA 平滑。"""
    if not anchors:
        return [], []
    ordered = sorted(anchors, key=lambda item: item.t.as_fraction())
    alpha = max(0.0, min(1.0, float(smoothing_alpha)))
    state: list[float] | None = None
    previous: FaceAnchor | None = None
    track_id = -1
    smoothed: list[FaceAnchor] = []
    for anchor in ordered:
        gap = (
            anchor.t.to_float() - previous.t.to_float()
            if previous is not None
            else float("inf")
        )
        same = (
            previous is not None
            and gap <= max_gap_seconds
            and (
                _iou(previous, anchor) >= iou_threshold
                or _center_gap(previous, anchor) <= 0.15
            )
        )
        if not same:
            track_id += 1
            state = [anchor.x, anchor.y, anchor.w, anchor.h]
        else:
            assert state is not None
            state = [
                alpha * anchor.x + (1 - alpha) * state[0],
                alpha * anchor.y + (1 - alpha) * state[1],
                alpha * anchor.w + (1 - alpha) * state[2],
                alpha * anchor.h + (1 - alpha) * state[3],
            ]
        x, y, w, h = _clamp_box(*state)
        smoothed.append(
            anchor.model_copy(
                update={
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "track_id": track_id,
                }
            )
        )
        previous = anchor
    return smoothed, derive_tracks(smoothed)


def _detect_frame(path: Path, cascade_path: Path, slot):
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover
        raise PerceptionUnavailable("需要 OpenCV；pip install -e '.[vision]'") from exc

    cascade = getattr(_THREAD_LOCAL, "cascade", None)
    if cascade is None:
        cascade = cv2.CascadeClassifier(str(cascade_path))
        _THREAD_LOCAL.cascade = cascade

    context = slot() if slot is not None else nullcontext()
    with context:
        image = cv2.imread(str(path))
        if image is None:
            return None
        height, width = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(30, 30),
        )
    if len(faces) == 0:
        return {"width": width, "height": height, "box": None}
    x, y, w, h = max(faces, key=lambda item: int(item[2]) * int(item[3]))
    return {"width": width, "height": height, "box": (int(x), int(y), int(w), int(h))}


def detect_faces(
    frames: list[FrameRef],
    workspace_root: Path,
    src: str,
    sampled_fps: float = 1.0,
    *,
    max_workers: int = 1,
    slot=None,
    process_pool_enabled: bool = False,
    process_workers: int = 0,
    pool_slot=None,
    cancel_check: Callable[[], None] | None = None,
    pool_timeout_seconds: float | None = None,
    tracking: bool = True,
    track_iou_threshold: float = 0.3,
    track_max_gap_seconds: float = 2.0,
    smoothing_alpha: float = 0.4,
) -> FaceReport:
    if cancel_check is not None:
        cancel_check()
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover
        raise PerceptionUnavailable("需要 OpenCV；pip install -e '.[vision]'") from exc

    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    if not cascade_path.exists():
        raise PerceptionUnavailable(f"缺少 Haar cascade: {cascade_path}")

    def detect(frame: FrameRef):
        return _detect_frame(workspace_root / frame.path, cascade_path, slot)

    workers = max(1, min(int(max_workers), len(frames) or 1))
    engine = "thread"
    results: list[dict | None] | None = None
    if (
        process_pool_enabled
        and pool_slot is not None
        and workers > 1
        and len(frames) > 1
    ):
        try:
            paths = [str(workspace_root / frame.path) for frame in frames]
            with pool_slot(workers):
                results = process_pool.map_faces(
                    paths,
                    str(cascade_path),
                    process_workers or workers,
                    cancel_check=cancel_check,
                    timeout_seconds=pool_timeout_seconds,
                )
            engine = "process"
        except (RunCancelledError, ToolExecutionError):
            raise
        except Exception:
            results = None
            engine = "thread_fallback"

    if results is None:
        if workers == 1:
            results = []
            for frame in frames:
                if cancel_check is not None:
                    cancel_check()
                results.append(detect(frame))
        else:
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="faces",
            ) as pool:
                results = []
                for start in range(0, len(frames), 32):
                    if cancel_check is not None:
                        cancel_check()
                    batch = frames[start : start + 32]
                    results.extend(pool.map(detect, batch))

    anchors: list[FaceAnchor] = []
    if cancel_check is not None:
        cancel_check()
    frame_width = 0
    frame_height = 0
    for frame, result in zip(frames, results):
        if result is None:
            continue
        width = result["width"]
        height = result["height"]
        frame_width, frame_height = width, height
        box = result["box"]
        if box is None:
            continue
        x, y, w, h = box
        anchors.append(
            FaceAnchor(
                t=frame.t,
                x=max(0.0, min(1.0, float(x) / width)),
                y=max(0.0, min(1.0, float(y) / height)),
                w=max(0.001, min(1.0, float(w) / width)),
                h=max(0.001, min(1.0, float(h) / height)),
                confidence=0.7,
            )
        )

    if tracking:
        anchors, tracks = track_anchors(
            anchors,
            iou_threshold=track_iou_threshold,
            max_gap_seconds=track_max_gap_seconds,
            smoothing_alpha=smoothing_alpha,
        )
    else:
        tracks = []
    return FaceReport(
        src=src,
        frame_width=frame_width,
        frame_height=frame_height,
        sampled_fps=sampled_fps,
        anchors=anchors,
        tracks=tracks,
        tracking_version=TRACKING_VERSION if tracking else "off",
        engine=engine,
    )
