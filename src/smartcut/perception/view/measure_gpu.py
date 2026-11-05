"""独立测量器：只输出数值 MeasureResult，与 renderer 解耦。

GPU 后端必须与 CPU 参考路径数值等价；失败/不可用必须回退 CPU 并显式记录。
只有真正加速的实现才允许提高候选窗口采样 fps（见 effective_measure_fps）。
"""

from __future__ import annotations

from typing import Any, Protocol

from smartcut.contracts.visual_evidence import MeasureResult
from smartcut.errors import RunCancelledError
from smartcut.logging import get_logger
from smartcut.perception.view.measure import measure_tiles

logger = get_logger("smartcut.perception.view")


class ViewMeasurer(Protocol):
    backend: str
    implementation: str
    accelerated: bool

    def measure(self, tiles: list[Any], **kwargs: Any) -> MeasureResult: ...


class ViewMeasurementUnavailable(RuntimeError):
    pass


class CpuViewMeasurer:
    backend = "cpu"
    implementation = "cpu"
    accelerated = False

    def measure(self, tiles: list[Any], **kwargs: Any) -> MeasureResult:
        return measure_tiles(tiles, **kwargs)


class CudaViewMeasurerPlaceholder:
    """CUDA 占位实现：只做能力探测，测量仍走 CPU 参考路径。

    当前 OpenCV Python 的 CUDA 直方图/缩放绑定不稳定，因此这里不提供任何
    GPU 加速，``accelerated=False``。``effective_measure_fps`` 只认
    ``accelerated``，该占位实现不会把采样率提升到 10–30fps，避免开启后
    抽帧量暴增却没有性能收益。真正的 GPU kernel 应另建实现并显式设置
    ``accelerated=True``，同时保持 MeasureResult 契约。
    """

    backend = "cuda-placeholder"
    implementation = "cuda-placeholder"
    accelerated = False

    def __init__(self) -> None:
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover - vision optional
            raise ViewMeasurementUnavailable("未安装 OpenCV") from exc
        cuda = getattr(cv2, "cuda", None)
        if cuda is None:
            raise ViewMeasurementUnavailable("OpenCV 未编译 CUDA 模块")
        try:
            if int(cuda.getCudaEnabledDeviceCount()) <= 0:
                raise ViewMeasurementUnavailable("没有可用的 CUDA 设备")
        except ViewMeasurementUnavailable:
            raise
        except Exception as exc:  # pragma: no cover - driver dependent
            raise ViewMeasurementUnavailable(
                f"CUDA 能力探测失败: {type(exc).__name__}: {exc}"
            ) from exc

    def measure(self, tiles: list[Any], **kwargs: Any) -> MeasureResult:
        # 数值契约与 CPU 完全一致；不参与 render_hash。
        return measure_tiles(tiles, **kwargs)


def build_view_measurer(settings) -> ViewMeasurer:
    if not getattr(settings, "gpu_measure_enabled", False):
        return CpuViewMeasurer()
    backend = str(getattr(settings, "gpu_measure_backend", "auto")).lower()
    if backend in {"cpu", "off", "false", "0"}:
        return CpuViewMeasurer()
    try:
        measurer = CudaViewMeasurerPlaceholder()
    except ViewMeasurementUnavailable:
        return CpuViewMeasurer()
    logger.warning(
        "cuda_measure_placeholder",
        detail="CUDA 测量当前为占位实现，使用 CPU 数值参考且不提升采样 fps",
    )
    return measurer


def _is_accelerated(measurer: ViewMeasurer | None) -> bool:
    return bool(getattr(measurer, "accelerated", False))


def effective_measure_fps(settings, measurer: ViewMeasurer | None) -> float:
    """只有真正加速的后端才允许 10–30fps；CPU/占位实现保持 view_fps。"""
    base = float(getattr(settings, "view_fps", 2.0))
    if getattr(settings, "gpu_measure_enabled", False) and _is_accelerated(
        measurer
    ):
        return max(base, min(30.0, float(getattr(settings, "gpu_measure_fps", 12.0))))
    return base


def measure_with_fallback(
    measurer: ViewMeasurer,
    tiles: list[Any],
    **kwargs: Any,
) -> tuple[MeasureResult, bool]:
    try:
        return measurer.measure(tiles, **kwargs), False
    except RunCancelledError:
        raise
    except Exception:
        if getattr(measurer, "backend", "cpu") == "cpu":
            raise
        return measure_tiles(tiles, **kwargs), True


def compare_measure_results(
    cpu: MeasureResult,
    gpu: MeasureResult,
    *,
    tolerance: float = 0.02,
) -> dict:
    """CPU/GPU 数值容差比对；像素/时间/bbox 必须完全一致。"""
    tolerance = max(0.0, float(tolerance))
    tiles_left = cpu.tile_map.tiles
    tiles_right = gpu.tile_map.tiles
    if len(tiles_left) != len(tiles_right):
        return {
            "within_tolerance": False,
            "reason": "tile_count_mismatch",
            "max_diff": float("inf"),
        }
    for left, right in zip(tiles_left, tiles_right):
        if (
            left.label != right.label
            or left.pts != right.pts
            or left.t != right.t
        ):
            return {
                "within_tolerance": False,
                "reason": "tile_identity_mismatch",
                "max_diff": float("inf"),
            }
    numeric_fields = (
        "d_hist",
        "flow_mag",
        "flow_dir_deg",
        "score",
        "camera_mag",
        "subject_mag",
    )
    exact_fields = ("bbox_xywh", "width", "height", "camera_motion")
    max_diff = 0.0
    diffs: dict[str, float] = {}
    for index, (left, right) in enumerate(
        zip(cpu.measurements, gpu.measurements)
    ):
        for field in numeric_fields:
            a = getattr(left, field)
            b = getattr(right, field)
            if a is None or b is None:
                if a != b:
                    return {
                        "within_tolerance": False,
                        "reason": f"{field}_presence_mismatch",
                        "max_diff": float("inf"),
                    }
                continue
            diff = abs(float(a) - float(b))
            diffs[f"{index}:{field}"] = diff
            max_diff = max(max_diff, diff)
        for field in exact_fields:
            if getattr(left, field) != getattr(right, field):
                return {
                    "within_tolerance": False,
                    "reason": f"{field}_mismatch",
                    "max_diff": float("inf"),
                }
    return {
        "within_tolerance": max_diff <= tolerance,
        "max_diff": round(max_diff, 6),
        "tolerance": tolerance,
        "diffs": diffs,
    }
