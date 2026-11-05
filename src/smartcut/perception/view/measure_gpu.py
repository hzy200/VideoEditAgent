"""独立测量器：只输出数值 MeasureResult，与 renderer 解耦。

GPU 后端必须与 CPU 参考路径数值等价；失败/不可用必须回退 CPU 并显式记录。
GPU 的真正价值是允许候选窗口用更高 fps 采样（见 effective_measure_fps）。
"""

from __future__ import annotations

from typing import Any, Protocol

from smartcut.contracts.visual_evidence import MeasureResult
from smartcut.errors import RunCancelledError
from smartcut.perception.view.measure import measure_tiles


class ViewMeasurer(Protocol):
    backend: str

    def measure(self, tiles: list[Any], **kwargs: Any) -> MeasureResult: ...


class ViewMeasurementUnavailable(RuntimeError):
    pass


class CpuViewMeasurer:
    backend = "cpu"

    def measure(self, tiles: list[Any], **kwargs: Any) -> MeasureResult:
        return measure_tiles(tiles, **kwargs)


class CudaViewMeasurer:
    """可选 CUDA 后端。

    当前 OpenCV Python 的 CUDA 直方图/缩放绑定不稳定，因此数值契约复用
    ``measure_tiles`` 作为 CPU 等价参考；该类负责能力探测与后端标记。
    真正的 GPU kernel 可在保持 MeasureResult 契约的前提下替换内部实现。
    """

    backend = "cuda"

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
        # 数值契约与 CPU 完全一致；backend 由调用方记录，不参与 render_hash。
        return measure_tiles(tiles, **kwargs)


def build_view_measurer(settings) -> ViewMeasurer:
    if not getattr(settings, "gpu_measure_enabled", False):
        return CpuViewMeasurer()
    backend = str(getattr(settings, "gpu_measure_backend", "auto")).lower()
    if backend in {"cpu", "off", "false", "0"}:
        return CpuViewMeasurer()
    try:
        return CudaViewMeasurer()
    except ViewMeasurementUnavailable:
        return CpuViewMeasurer()


def effective_measure_fps(settings, measurer: ViewMeasurer | None) -> float:
    """GPU 测量可用时允许 10–30fps 密度；CPU 保持配置的 view_fps。"""
    base = float(getattr(settings, "view_fps", 2.0))
    if (
        getattr(settings, "gpu_measure_enabled", False)
        and getattr(measurer, "backend", "cpu") != "cpu"
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
