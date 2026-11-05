"""帧差/光流可视化：hsv_hue_v1 + flow_x/flow_y 双通道。

只做测量与颜色映射，不产出切点；任一帧不可读时跳过。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from io import BytesIO

from smartcut.errors import PerceptionUnavailable

WORK_SIZE = (160, 90)
FLOW_MAG_CAP_RATIO = 0.05
FLOW_MAG_FLOOR_RATIO = 0.002


@dataclass(frozen=True)
class FlowResult:
    diff_rgb: bytes
    hue_rgb: bytes
    flow_x_gray: bytes
    flow_y_gray: bytes
    flow_mag: float
    flow_dir_deg: float
    mag_cap: float
    mag_floor: float
    camera_mag: float = 0.0
    subject_mag: float = 0.0
    camera_motion: str = "unknown"
    camera_confidence: float = 0.0


def _split_motion(prev_gray, curr_gray, flow, cv2, np) -> dict:
    """用 RANSAC 仿射把光流拆成全局相机项与残差主体项。"""
    height, width = prev_gray.shape[:2]
    step = 16
    ys, xs = np.mgrid[step // 2 : height : step, step // 2 : width : step]
    if xs.size < 4:
        return {}
    src = np.stack([xs.ravel(), ys.ravel()], axis=1).astype(np.float32)
    dst = src + flow[ys.ravel(), xs.ravel()]
    try:
        matrix, inliers = cv2.estimateAffinePartial2D(
            src,
            dst,
            method=cv2.RANSAC,
            ransacReprojThreshold=2.0,
            maxIters=64,
            confidence=0.95,
        )
    except Exception:
        return {}
    if matrix is None:
        return {}
    ones = np.ones((len(src), 1), dtype=np.float32)
    predicted = np.hstack([src, ones]) @ matrix.T
    global_disp = predicted - src
    residual = dst - predicted
    camera_mag = float(np.linalg.norm(global_disp, axis=1).mean())
    subject_mag = float(np.linalg.norm(residual, axis=1).mean())
    scale = float(math.hypot(matrix[0, 0], matrix[1, 0]))
    tx = float(matrix[0, 2])
    ty = float(matrix[1, 2])
    if camera_mag < 0.2:
        motion = "static"
    elif abs(scale - 1.0) > 0.02:
        motion = "zoom"
    elif abs(tx) > abs(ty) * 1.2:
        motion = "pan"
    elif abs(ty) > abs(tx) * 1.2:
        motion = "tilt"
    else:
        motion = "camera"
    confidence = (
        float(np.asarray(inliers).mean()) if inliers is not None else 0.0
    )
    return {
        "camera_mag": round(camera_mag, 6),
        "subject_mag": round(subject_mag, 6),
        "camera_motion": motion,
        "camera_confidence": round(min(1.0, max(0.0, confidence)), 4),
    }


def _cv():
    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - vision optional
        raise PerceptionUnavailable("需要 OpenCV；pip install -e '.[vision]'") from exc
    return cv2, np


def gray(data: bytes, size: tuple[int, int] = WORK_SIZE):
    cv2, np = _cv()
    from PIL import Image

    with Image.open(BytesIO(data)) as image:
        image = image.convert("RGB").resize(size, Image.Resampling.BILINEAR)
        rgb = np.asarray(image, dtype=np.uint8)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def _encode_rgb(rgb) -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def _encode_gray(gray_image) -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.fromarray(gray_image, mode="L").save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def flow_between(
    previous: bytes,
    current: bytes,
    *,
    size: tuple[int, int] = WORK_SIZE,
) -> FlowResult:
    cv2, np = _cv()
    prev_gray = gray(previous, size)
    curr_gray = gray(current, size)
    diff = cv2.absdiff(curr_gray, prev_gray)
    diff_rgb = cv2.cvtColor(diff, cv2.COLOR_GRAY2RGB)
    flow = cv2.calcOpticalFlowFarneback(
        prev_gray,
        curr_gray,
        None,
        0.5,
        3,
        15,
        3,
        5,
        1.2,
        0,
    )
    dx = flow[..., 0]
    dy = flow[..., 1]
    magnitude = np.hypot(dx, dy)
    angle = np.arctan2(dy, dx)
    cap = max(1.0, FLOW_MAG_CAP_RATIO * max(size))
    floor = max(0.01, FLOW_MAG_FLOOR_RATIO * max(size))
    hue = ((angle + math.pi) / (2 * math.pi) * 180).astype(np.uint8)
    saturation = np.full(hue.shape, 255, dtype=np.uint8)
    value = np.clip(magnitude / cap, 0, 1).astype(np.float32) * 255
    value = value.astype(np.uint8)
    value[magnitude < floor] = 0
    hsv = cv2.merge([hue, saturation, value])
    hue_rgb = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
    flow_x = np.clip((dx / cap + 1.0) * 127.5, 0, 255).astype(np.uint8)
    flow_y = np.clip((dy / cap + 1.0) * 127.5, 0, 255).astype(np.uint8)
    split = _split_motion(prev_gray, curr_gray, flow, cv2, np)
    return FlowResult(
        diff_rgb=_encode_rgb(diff_rgb),
        hue_rgb=_encode_rgb(hue_rgb),
        flow_x_gray=_encode_gray(flow_x),
        flow_y_gray=_encode_gray(flow_y),
        flow_mag=round(float(magnitude.mean()), 6),
        flow_dir_deg=round(float(np.degrees(angle).mean()), 3),
        mag_cap=round(cap, 4),
        mag_floor=round(floor, 4),
        camera_mag=float(split.get("camera_mag", 0.0)),
        subject_mag=float(split.get("subject_mag", 0.0)),
        camera_motion=str(split.get("camera_motion", "unknown")),
        camera_confidence=float(split.get("camera_confidence", 0.0)),
    )


def composite_temporal(flow: FlowResult, tile_width: int, tile_height: int) -> bytes:
    """2x2 拼图：当前帧、帧差、flow_x、flow_y 由调用方补当前帧。"""
    from PIL import Image, ImageOps

    size = (tile_width, tile_height)
    panels = [
        Image.open(BytesIO(flow.diff_rgb)).convert("RGB"),
        Image.open(BytesIO(flow.hue_rgb)).convert("RGB"),
        Image.open(BytesIO(flow.flow_x_gray)).convert("RGB"),
        Image.open(BytesIO(flow.flow_y_gray)).convert("RGB"),
    ]
    canvas = Image.new("RGB", (tile_width * 2, tile_height * 2), "#111827")
    for index, panel in enumerate(panels):
        canvas.paste(
            ImageOps.fit(panel, size, method=Image.Resampling.BILINEAR),
            ((index % 2) * tile_width, (index // 2) * tile_height),
        )
    buffer = BytesIO()
    canvas.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def temporal_composite(
    previous: bytes,
    current: bytes,
    tile_width: int,
    tile_height: int,
) -> tuple[bytes, FlowResult]:
    from PIL import Image, ImageOps

    flow = flow_between(previous, current)
    panels = [
        Image.open(BytesIO(current)).convert("RGB"),
        Image.open(BytesIO(flow.diff_rgb)).convert("RGB"),
        Image.open(BytesIO(flow.hue_rgb)).convert("RGB"),
        Image.open(BytesIO(flow.flow_x_gray)).convert("RGB"),
        Image.open(BytesIO(flow.flow_y_gray)).convert("RGB"),
    ]
    # 3x2 网格：当前帧 + 帧差 + hue + flow_x + flow_y。
    canvas = Image.new("RGB", (tile_width * 3, tile_height * 2), "#111827")
    for index, panel in enumerate(panels):
        canvas.paste(
            ImageOps.fit(panel, (tile_width, tile_height), method=Image.Resampling.BILINEAR),
            ((index % 3) * tile_width, (index // 3) * tile_height),
        )
    buffer = BytesIO()
    canvas.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue(), flow


def flow_strip(
    flows: list[FlowResult],
    tile_width: int,
    tile_height: int,
) -> bytes:
    """沿轨迹堆叠：左 flow_x、中 flow_y、右 hue。"""
    from PIL import Image, ImageOps

    flows = flows[:4] or []
    height = max(1, len(flows)) * tile_height
    canvas = Image.new("RGB", (tile_width * 3, height), "#111827")
    for index, flow in enumerate(flows):
        columns = [
            Image.open(BytesIO(flow.flow_x_gray)).convert("RGB"),
            Image.open(BytesIO(flow.flow_y_gray)).convert("RGB"),
            Image.open(BytesIO(flow.hue_rgb)).convert("RGB"),
        ]
        for column, panel in enumerate(columns):
            canvas.paste(
                ImageOps.fit(
                    panel,
                    (tile_width, tile_height),
                    method=Image.Resampling.BILINEAR,
                ),
                (column * tile_width, index * tile_height),
            )
    buffer = BytesIO()
    canvas.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def crop_roi(
    image_bytes: bytes,
    rect: tuple[float, float, float, float],
    *,
    target: int = 512,
    margin: float = 0.25,
) -> tuple[bytes, tuple[int, int, int, int]]:
    """按归一化 bbox 扩边裁切回原图；返回 JPEG 与源图像素 bbox。"""
    from PIL import Image, ImageOps

    with Image.open(BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
        width, height = image.size
        x, y, w, h = rect
        pad_x = w * margin
        pad_y = h * margin
        left = max(0.0, x - pad_x)
        top = max(0.0, y - pad_y)
        right = min(1.0, x + w + pad_x)
        bottom = min(1.0, y + h + pad_y)
        box = (
            int(round(left * width)),
            int(round(top * height)),
            max(int(round(left * width)) + 2, int(round(right * width))),
            max(int(round(top * height)) + 2, int(round(bottom * height))),
        )
        crop = image.crop(box)
        square = ImageOps.fit(
            crop,
            (target, target),
            method=Image.Resampling.LANCZOS,
        )
        buffer = BytesIO()
        square.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue(), box
