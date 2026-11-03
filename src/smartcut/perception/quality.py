"""帧质量：黑帧、静帧、糊帧、重复帧。

静态度量可按帧并行；冻结/重复判断保持顺序，使用下采样灰度图降低内存。
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

from smartcut.contracts.perception import FrameRef
from smartcut.contracts.quality import QualityMark, QualityReport
from smartcut.errors import (
    PerceptionUnavailable,
    RunCancelledError,
    ToolExecutionError,
)
from smartcut.perception import process_pool

_BATCH = 32


def _frame_metrics(path: Path, cv2, np, slot):
    context = slot() if slot is not None else nullcontext()
    with context:
        image = cv2.imread(str(path))
        if image is None:
            return None
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (160, 90), interpolation=cv2.INTER_AREA)
        return {
            "mean": float(gray.mean()),
            "std": float(gray.std()),
            "laplacian": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
            "hash": _dhash(gray, cv2, np),
            "gray": small,
        }


def analyze_quality(
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
) -> QualityReport:
    if cancel_check is not None:
        cancel_check()
    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover
        raise PerceptionUnavailable("需要 OpenCV；pip install -e '.[vision]'") from exc

    marks: list[QualityMark] = []
    previous_gray = None
    previous_hash = None
    last_kind: str | None = None
    last_time = -10.0

    def analyze_metric(frame: FrameRef, metric: dict) -> None:
        nonlocal previous_gray, previous_hash, last_kind, last_time
        gray = metric["gray"]
        mean = metric["mean"]
        std = metric["std"]
        laplacian = metric["laplacian"]
        seconds = frame.t.to_float()

        candidates: list[tuple[str, float, str]] = []
        if mean < 25.0 and std < 20.0:
            candidates.append(
                ("black", min(1.0, (25.0 - mean) / 25.0 + 0.5), f"mean={mean:.1f}")
            )
        if laplacian < 50.0:
            candidates.append(
                ("blurry", min(1.0, (50.0 - laplacian) / 50.0), f"lap={laplacian:.1f}")
            )
        if previous_gray is not None:
            diff = float(np.mean(cv2.absdiff(gray, previous_gray)))
            if diff < 1.0 and mean >= 25.0:
                candidates.append(("frozen", min(1.0, 1.0 - diff), f"diff={diff:.2f}"))
        current_hash = metric["hash"]
        if previous_hash is not None and _hamming(current_hash, previous_hash) <= 2:
            candidates.append(("duplicate", 0.8, "hash distance <= 2"))
        previous_gray = gray
        previous_hash = current_hash

        for kind, score, detail in candidates:
            if kind == last_kind and seconds - last_time < 1.0:
                continue
            marks.append(
                QualityMark(
                    t=frame.t,
                    kind=kind,
                    score=max(0.0, min(1.0, score)),
                    detail=detail,
                )
            )
            last_kind = kind
            last_time = seconds

    def metric_for(frame: FrameRef):
        return _frame_metrics(workspace_root / frame.path, cv2, np, slot)

    workers = max(1, min(int(max_workers), len(frames) or 1))
    engine = "thread"
    metrics: list[dict | None] | None = None
    if (
        process_pool_enabled
        and pool_slot is not None
        and workers > 1
        and len(frames) > 1
    ):
        try:
            paths = [str(workspace_root / frame.path) for frame in frames]
            with pool_slot(workers):
                raw = process_pool.map_quality(
                    paths,
                    process_workers or workers,
                    cancel_check=cancel_check,
                    timeout_seconds=pool_timeout_seconds,
                )
            metrics = []
            for item in raw:
                if item is None:
                    metrics.append(None)
                    continue
                gray_bytes = item.pop("gray_bytes", None)
                shape = item.pop("gray_shape", (90, 160))
                if gray_bytes is not None:
                    item["gray"] = np.frombuffer(
                        gray_bytes,
                        dtype=np.uint8,
                    ).reshape(shape)
                metrics.append(item)
            engine = "process"
        except (RunCancelledError, ToolExecutionError):
            raise
        except Exception:
            metrics = None
            engine = "thread_fallback"

    if metrics is not None:
        for frame, metric in zip(frames, metrics):
            if cancel_check is not None:
                cancel_check()
            if metric is not None:
                analyze_metric(frame, metric)
    elif workers == 1:
        for frame in frames:
            if cancel_check is not None:
                cancel_check()
            metric = metric_for(frame)
            if metric is not None:
                analyze_metric(frame, metric)
    else:
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="quality",
        ) as pool:
            for start in range(0, len(frames), _BATCH):
                if cancel_check is not None:
                    cancel_check()
                batch = frames[start : start + _BATCH]
                metrics = list(pool.map(metric_for, batch))
                for frame, metric in zip(batch, metrics):
                    if cancel_check is not None:
                        cancel_check()
                    if metric is not None:
                        analyze_metric(frame, metric)

    return QualityReport(
        src=src,
        frame_count=len(frames),
        sampled_fps=sampled_fps,
        marks=marks,
        engine=engine,
    )


def _dhash(gray, cv2, np) -> int:
    resized = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (resized[:, 1:] > resized[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def _hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()
