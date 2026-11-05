"""屏幕文字：cv2 候选区检测 + 外部进程 OCR 适配 + 时间稳定性聚合。

主进程不 import OCR 重依赖；OCR 引擎只在 worker 进程加载。
结果只作为候选事实，不写 IR/时间线。
"""

from __future__ import annotations

import hashlib
import multiprocessing
import sys
import time
from multiprocessing import TimeoutError as MPTimeoutError
from typing import Any, Protocol

from smartcut.contracts.perception import FrameRef
from smartcut.contracts.screen_text import OCR_VERSION, ScreenTextEvidence
from smartcut.errors import ToolExecutionError


class ScreenTextExtractor(Protocol):
    version: str

    def analyze(
        self,
        paths: list[str],
        *,
        cancel_check: Any | None = None,
        timeout_seconds: float | None = None,
    ) -> list[dict]: ...


class NullScreenTextExtractor:
    version = f"{OCR_VERSION}:unavailable"

    def analyze(self, paths, *, cancel_check=None, timeout_seconds=None):
        return [
            {
                "engine": "unavailable",
                "items": [],
                "error": "OCR 未配置（pip install -e '.[ocr]'）",
            }
            for _ in paths
        ]


def build_screen_text_extractor(settings) -> ScreenTextExtractor:
    if not getattr(settings, "ocr_enabled", False):
        return NullScreenTextExtractor()
    return ProcessScreenTextExtractor(engine=settings.ocr_engine)


class ProcessScreenTextExtractor:
    """独立进程 OCR；worker 内才 import rapidocr/pytesseract。"""

    def __init__(self, engine: str = "auto") -> None:
        self.engine = engine or "auto"
        self.version = f"{OCR_VERSION}:{self.engine}"

    def analyze(
        self,
        paths: list[str],
        *,
        cancel_check: Any | None = None,
        timeout_seconds: float | None = None,
    ) -> list[dict]:
        if not paths:
            return []
        if getattr(sys, "frozen", False):
            raise ToolExecutionError(
                "打包桌面未内置 OCR worker；请使用源码环境或外部 worker"
            )
        from smartcut.ocr_worker import ocr_batch

        context = multiprocessing.get_context("spawn")
        pool = context.Pool(processes=1)
        try:
            pending = pool.apply_async(ocr_batch, (list(paths), self.engine))
            deadline = (
                time.monotonic() + max(0.01, float(timeout_seconds))
                if timeout_seconds is not None
                else None
            )
            while True:
                if cancel_check is not None:
                    cancel_check()
                wait = 0.2
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ToolExecutionError(
                            f"OCR worker 超时（>{timeout_seconds}s）"
                        )
                    wait = min(0.2, max(0.01, remaining))
                try:
                    return pending.get(timeout=wait)
                except MPTimeoutError:
                    continue
        except BaseException:
            pool.terminate()
            pool.join()
            raise
        else:
            pool.close()
            pool.join()


def _cv():
    try:
        import cv2
        import numpy as np
    except ImportError:  # pragma: no cover - vision optional
        return None, None
    return cv2, np


def _iou(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[0] + left[2], right[0] + right[2])
    y2 = min(left[1] + left[3], right[1] + right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = left[2] * left[3] + right[2] * right[3] - intersection
    return intersection / union if union > 0 else 0.0


def detect_text_regions(
    image_bytes: bytes,
    *,
    max_regions: int = 6,
    min_area_ratio: float = 0.0005,
    aspect_min: float = 1.2,
    aspect_max: float = 20.0,
) -> list[tuple[float, float, float, float]]:
    """形态学梯度 + 轮廓长宽比筛选文字候选区（归一化）。"""
    cv2, np = _cv()
    if cv2 is None or np is None:
        return []
    try:
        buffer = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        if image is None:
            return []
        height, width = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        gradient = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, kernel)
        _threshold, binary = cv2.threshold(
            gradient, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU
        )
        closed = cv2.morphologyEx(
            binary,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (9, 3)),
        )
        contours, _hierarchy = cv2.findContours(
            closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
    except Exception:
        return []
    regions: list[tuple[float, float, float, float, float]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w <= 2 or h <= 2:
            continue
        area_ratio = (w * h) / float(width * height)
        aspect = max(w / h, h / w)
        if area_ratio < min_area_ratio or area_ratio > 0.5:
            continue
        if aspect < aspect_min or aspect > aspect_max:
            continue
        regions.append(
            (
                x / width,
                y / height,
                w / width,
                h / height,
                float(w * h),
            )
        )
    regions.sort(key=lambda item: item[4], reverse=True)
    accepted: list[tuple[float, float, float, float]] = []
    for region in regions:
        box = region[:4]
        if any(_iou(box, other) >= 0.3 for other in accepted):
            continue
        accepted.append(box)
        if len(accepted) >= max_regions:
            break
    return accepted


def _accepted_by_region(
    bbox: tuple[float, float, float, float],
    regions: list[tuple[float, float, float, float]] | None,
) -> bool:
    if not regions:
        # 候选区缺失时不阻断 OCR，只做稳定性/置信度过滤。
        return True
    return any(_iou(bbox, region) >= 0.2 for region in regions)


def aggregate_screen_text(
    frames: list[FrameRef],
    results: list[dict],
    *,
    regions_by_index: list[list[tuple[float, float, float, float]]] | None = None,
    min_confidence: float = 0.5,
    min_stability: float = 0.0,
    max_items: int = 200,
) -> list[ScreenTextEvidence]:
    occurrences: dict[str, list[dict]] = {}
    total_frames = max(1, len(frames))
    for index, (frame, result) in enumerate(zip(frames, results)):
        regions = (
            regions_by_index[index]
            if regions_by_index is not None and index < len(regions_by_index)
            else None
        )
        for item in (result or {}).get("items") or []:
            text = " ".join(str(item.get("text") or "").split())
            if not text:
                continue
            try:
                confidence = float(item.get("confidence") or 0.0)
                bbox = tuple(float(value) for value in item.get("bbox") or ())
            except (TypeError, ValueError):
                continue
            if len(bbox) != 4 or confidence < min_confidence:
                continue
            x, y, w, h = bbox
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and w > 0 and h > 0):
                continue
            w = min(w, 1.0 - x)
            h = min(h, 1.0 - y)
            if w <= 0 or h <= 0:
                continue
            box = (x, y, w, h)
            if not _accepted_by_region(box, regions):
                continue
            occurrences.setdefault(text, []).append(
                {
                    "t": frame.t,
                    "bbox": box,
                    "confidence": confidence,
                }
            )
    items: list[ScreenTextEvidence] = []
    for text, values in occurrences.items():
        best = max(values, key=lambda value: value["confidence"])
        stability = min(1.0, len(values) / total_frames)
        if stability < min_stability:
            continue
        first = min(values, key=lambda value: value["t"].as_fraction())
        bbox = first["bbox"]
        digest = hashlib.sha256(
            (
                f"{text}|{bbox[0]:.6f},{bbox[1]:.6f},"
                f"{bbox[2]:.6f},{bbox[3]:.6f}|"
                f"{first['t'].value}/{first['t'].rate}"
            ).encode("utf-8")
        ).hexdigest()
        items.append(
            ScreenTextEvidence(
                t=first["t"],
                text=text,
                bbox_norm=bbox,
                confidence=round(best["confidence"], 4),
                stability=round(stability, 4),
                sha256=digest,
            )
        )
    items.sort(key=lambda item: (item.t.as_fraction(), item.text))
    return items[: max(1, int(max_items))]
