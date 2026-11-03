"""人脸锚点：OpenCV Haar cascade（归一化 bbox），按帧并行检测。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

from smartcut.contracts.faces import FaceAnchor, FaceReport
from smartcut.contracts.perception import FrameRef
from smartcut.errors import (
    PerceptionUnavailable,
    RunCancelledError,
    ToolExecutionError,
)
from smartcut.perception import process_pool

_THREAD_LOCAL = threading.local()


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

    return FaceReport(
        src=src,
        frame_width=frame_width,
        frame_height=frame_height,
        sampled_fps=sampled_fps,
        anchors=anchors,
        engine=engine,
    )
