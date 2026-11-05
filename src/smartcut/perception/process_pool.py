"""OpenCV 逐帧分析的进程池后端（Windows 使用 spawn）。

进程池只在后台服务/worker 的长任务启用；父进程持有 ResourceManager 的
CPU 配额，worker 内不直接操作共享状态或 SQLite。
"""

from __future__ import annotations

import multiprocessing
import sys
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import TimeoutError as MPTimeoutError

from smartcut.errors import ToolExecutionError
from smartcut.perception.view import fingerprint

_CASCADE = None


def should_use_process_pool(settings, frame_count: int) -> bool:
    mode = str(getattr(settings, "perception_process_pool", "auto")).lower()
    if mode in {"off", "false", "0", "no", "none"}:
        return False
    if getattr(sys, "frozen", False):
        # 打包桌面默认不走进程池，避免 freeze_support/子进程启动风险。
        return False
    if mode in {"on", "true", "1", "yes"}:
        return frame_count > 1
    return frame_count >= int(
        getattr(settings, "perception_process_pool_min_frames", 240)
    )


def _worker_count(requested: int, count: int) -> int:
    return max(1, min(int(requested or 1), count or 1))


def map_quality(
    paths: list[str],
    workers: int,
    *,
    cancel_check: Callable[[], None] | None = None,
    timeout_seconds: float | None = None,
) -> list[dict | None]:
    if cancel_check is not None or timeout_seconds is not None:
        return _map_interruptible(
            _quality_item,
            list(enumerate(paths)),
            len(paths),
            workers,
            cancel_check,
            timeout_seconds,
        )
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=_worker_count(workers, len(paths)),
        mp_context=context,
    ) as pool:
        return list(
            pool.map(
                quality_metric,
                paths,
                chunksize=max(1, len(paths) // (max(1, workers) * 4) or 1),
            )
        )


def map_faces(
    paths: list[str],
    cascade_path: str,
    workers: int,
    *,
    cancel_check: Callable[[], None] | None = None,
    timeout_seconds: float | None = None,
) -> list[dict | None]:
    if cancel_check is not None or timeout_seconds is not None:
        return _map_interruptible(
            _face_item,
            [(index, path, cascade_path) for index, path in enumerate(paths)],
            len(paths),
            workers,
            cancel_check,
            timeout_seconds,
        )
    context = multiprocessing.get_context("spawn")
    payload = [(path, cascade_path) for path in paths]
    with ProcessPoolExecutor(
        max_workers=_worker_count(workers, len(paths)),
        mp_context=context,
    ) as pool:
        return list(
            pool.map(
                face_metric,
                payload,
                chunksize=max(1, len(paths) // (max(1, workers) * 4) or 1),
            )
        )


def _quality_item(payload: tuple[int, str]) -> tuple[int, dict | None]:
    index, path = payload
    return index, quality_metric(path)


def _face_item(payload: tuple[int, str, str]) -> tuple[int, dict | None]:
    index, path, cascade_path = payload
    return index, face_metric((path, cascade_path))


def _map_interruptible(
    worker,
    payload: list,
    count: int,
    workers: int,
    cancel_check: Callable[[], None] | None,
    timeout_seconds: float | None,
) -> list:
    """可中断的进程池：取消/超时时 terminate 整个池，而不是等 map 返回。"""
    context = multiprocessing.get_context("spawn")
    pool = context.Pool(processes=_worker_count(workers, count))
    results: list = [None] * count
    deadline = (
        time.monotonic() + max(0.01, float(timeout_seconds))
        if timeout_seconds is not None
        else None
    )
    try:
        iterator = pool.imap_unordered(worker, payload, chunksize=1)
        remaining = count
        while remaining:
            if cancel_check is not None:
                cancel_check()
            wait_timeout = 0.2
            if deadline is not None:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise ToolExecutionError(
                        f"感知进程池超时（>{timeout_seconds}s）"
                    )
                wait_timeout = min(0.2, max(0.01, remaining_time))
            try:
                index, value = iterator.next(timeout=wait_timeout)
            except MPTimeoutError:
                continue
            results[index] = value
            remaining -= 1
    except BaseException:
        pool.terminate()
        pool.join()
        raise
    else:
        pool.close()
        pool.join()
    return results


def quality_metric(path: str) -> dict | None:
    try:
        import cv2
        import numpy as np
    except ImportError:  # pragma: no cover
        return None
    image = cv2.imread(path)
    if image is None:
        return None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (160, 90), interpolation=cv2.INTER_AREA)
    return {
        "mean": float(gray.mean()),
        "std": float(gray.std()),
        "laplacian": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
        "hash": fingerprint.dhash_gray(gray, cv2, np),
        "gray_bytes": small.tobytes(),
        "gray_shape": small.shape,
    }


def face_metric(payload: tuple[str, str]) -> dict | None:
    path, cascade_path = payload
    try:
        import cv2
    except ImportError:  # pragma: no cover
        return None
    global _CASCADE
    if _CASCADE is None:
        _CASCADE = cv2.CascadeClassifier(cascade_path)
    image = cv2.imread(path)
    if image is None:
        return None
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    faces = _CASCADE.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(30, 30),
    )
    if len(faces) == 0:
        return {"width": width, "height": height, "box": None}
    x, y, w, h = max(faces, key=lambda item: int(item[2]) * int(item[3]))
    return {
        "width": width,
        "height": height,
        "box": (int(x), int(y), int(w), int(h)),
    }
