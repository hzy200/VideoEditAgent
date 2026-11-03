"""9:16 裁切计算（纯函数）。

模型只给 anchor（face_bbox/center），坐标由这里根据感知结果与源尺寸推导。
"""

from __future__ import annotations

from dataclasses import dataclass

from smartcut.contracts.faces import FaceReport

TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
TARGET_RATIO = TARGET_WIDTH / TARGET_HEIGHT


@dataclass(frozen=True)
class FitCrop:
    crop_x: int
    crop_y: int
    crop_width: int
    crop_height: int
    target_width: int = TARGET_WIDTH
    target_height: int = TARGET_HEIGHT


def _even(value: int) -> int:
    return value - (value % 2)


def compute_fit_crop(
    source_width: int,
    source_height: int,
    faces: FaceReport | None = None,
    *,
    use_faces: bool = True,
) -> FitCrop:
    if source_width <= 0 or source_height <= 0:
        raise ValueError("源尺寸必须为正")

    source_ratio = source_width / source_height
    if source_ratio > TARGET_RATIO:
        crop_height = source_height
        crop_width = int(round(crop_height * TARGET_RATIO))
    else:
        crop_width = source_width
        crop_height = int(round(crop_width / TARGET_RATIO))

    crop_width = max(2, _even(min(crop_width, source_width)))
    crop_height = max(2, _even(min(crop_height, source_height)))

    center_x = 0.5
    center_y = 0.5
    if use_faces and faces is not None and faces.anchors:
        center_x = sum(anchor.center_x for anchor in faces.anchors) / len(faces.anchors)
        center_y = sum(anchor.center_y for anchor in faces.anchors) / len(faces.anchors)

    x = int(round(center_x * source_width - crop_width / 2))
    y = int(round(center_y * source_height - crop_height / 2))
    x = max(0, min(x, source_width - crop_width))
    y = max(0, min(y, source_height - crop_height))
    x = _even(x)
    y = _even(y)

    return FitCrop(
        crop_x=x,
        crop_y=y,
        crop_width=crop_width,
        crop_height=crop_height,
    )
