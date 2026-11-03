"""镜头切分：PySceneDetect 自适应检测，失败时回退 OpenCV 分位阈值。"""

from __future__ import annotations

import logging
from pathlib import Path

from smartcut.contracts.shots import ShotBoundary, ShotList
from smartcut.contracts.time import RationalTime
from smartcut.errors import PerceptionUnavailable


def detect_scenes(video_path: Path, src: str) -> ShotList:
    try:
        import cv2
        import numpy as np
        from scenedetect import AdaptiveDetector, SceneManager, open_video
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        raise PerceptionUnavailable(
            "需要 opencv-python 与 scenedetect；pip install -e '.[vision]'"
        ) from exc

    logging.getLogger("scenedetect").setLevel(logging.WARNING)

    shots: list[ShotBoundary] = []
    try:
        video = open_video(str(video_path))
        manager = SceneManager()
        manager.add_detector(AdaptiveDetector(adaptive_threshold=3.0, min_scene_len=15))
        manager.detect_scenes(video=video, show_progress=False)
        for start, _end in manager.get_scene_list():
            seconds = float(start.get_seconds())
            if seconds <= 0:
                continue
            shots.append(
                ShotBoundary(
                    t=RationalTime.from_seconds(seconds, rate=90_000),
                    confidence=0.8,
                    type="hard",
                )
            )
    except Exception:
        shots = []

    if not shots:
        shots = _percentile_cuts(video_path, cv2, np)

    shots.sort(key=lambda item: item.t.as_fraction())
    return ShotList(src=src, shots=shots)


def _percentile_cuts(video_path: Path, cv2, np) -> list[ShotBoundary]:
    """OpenCV HSV 直方图差异 + 全片第 70 百分位阈值。"""
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return []
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(fps)))
    diffs: list[tuple[float, float]] = []
    previous = None
    index = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        if index % step == 0:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            histogram = cv2.calcHist(
                [hsv], [0, 1], None, [32, 32], [0, 180, 0, 256]
            )
            histogram = cv2.normalize(histogram, histogram).flatten()
            if previous is not None:
                correlation = float(cv2.compareHist(previous, histogram, cv2.HISTCMP_CORREL))
                distance = max(0.0, 1.0 - correlation)
                diffs.append((index / fps, distance))
            previous = histogram
        index += 1
    capture.release()
    if not diffs:
        return []

    values = np.array([distance for _, distance in diffs], dtype=np.float32)
    threshold = max(float(np.percentile(values, 70)), 0.25)
    cuts: list[ShotBoundary] = []
    for position, (seconds, distance) in enumerate(diffs):
        before = diffs[position - 1][1] if position > 0 else 0.0
        after = diffs[position + 1][1] if position + 1 < len(diffs) else 0.0
        if distance < threshold or distance < before or distance < after:
            continue
        confidence = min(1.0, max(0.0, (distance - threshold) / max(1.0 - threshold, 0.01)))
        cuts.append(
            ShotBoundary(
                t=RationalTime.from_seconds(seconds, rate=90_000),
                confidence=max(0.5, confidence),
                type="hard",
            )
        )
    return cuts
