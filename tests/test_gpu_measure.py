from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from smartcut.config import Settings
from smartcut.contracts.time import RationalTime
from smartcut.contracts.visual_evidence import compute_render_hash
from smartcut.perception.transcribe import NullTranscriber
from smartcut.perception.view.measure import TileInput, measure_tiles
from smartcut.perception.view.measure_gpu import (
    CpuViewMeasurer,
    CudaViewMeasurerPlaceholder,
    build_view_measurer,
    compare_measure_results,
    effective_measure_fps,
    measure_with_fallback,
)
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _image(offset: int) -> bytes:
    image = Image.new("RGB", (160, 90), (20, 30, 40))
    draw = ImageDraw.Draw(image)
    draw.rectangle((10 + offset, 20, 80 + offset, 60), fill=(240, 240, 240))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _measure():
    return measure_tiles(
        [
            TileInput(t=_rt("0"), pts=0, frame_no=0, image_bytes=_image(0)),
            TileInput(t=_rt("1"), pts=30, frame_no=30, image_bytes=_image(20)),
        ],
        content_id="p_gpu",
        time_base="1/30",
        grid_cols=2,
    )


class _NoisyGpu:
    backend = "cuda"

    def __init__(self, delta: float) -> None:
        self.delta = delta

    def measure(self, tiles, **kwargs):
        result = measure_tiles(tiles, **kwargs)
        measurements = [
            item.model_copy(
                update={
                    "d_hist": min(1.0, item.d_hist + self.delta),
                    "score": min(1.0, item.score + self.delta),
                }
            )
            for item in result.measurements
        ]
        return result.model_copy(update={"measurements": measurements})


class _FailingGpu:
    backend = "cuda"

    def measure(self, tiles, **kwargs):
        raise RuntimeError("gpu kernel failed")


def test_cpu_measurer_matches_reference() -> None:
    tiles = [
        TileInput(t=_rt("0"), pts=0, frame_no=0, image_bytes=_image(0)),
        TileInput(t=_rt("1"), pts=30, frame_no=30, image_bytes=_image(20)),
    ]
    expected = measure_tiles(tiles, content_id="p_gpu", time_base="1/30")
    actual = CpuViewMeasurer().measure(
        tiles, content_id="p_gpu", time_base="1/30"
    )
    comparison = compare_measure_results(expected, actual)
    assert comparison["within_tolerance"] is True
    assert comparison["max_diff"] == 0.0


def test_gpu_measure_tolerance_and_mismatch() -> None:
    cpu = _measure()
    close = _NoisyGpu(0.01).measure(
        [
            TileInput(t=item.t, pts=item.pts, frame_no=item.frame_no, image_bytes=payload)
            for item, payload in zip(cpu.tile_map.tiles, cpu.tile_images)
        ],
        content_id="p_gpu",
        time_base="1/30",
        grid_cols=2,
    )
    assert compare_measure_results(cpu, close, tolerance=0.02)[
        "within_tolerance"
    ]
    far = _NoisyGpu(0.1).measure(
        [
            TileInput(t=item.t, pts=item.pts, frame_no=item.frame_no, image_bytes=payload)
            for item, payload in zip(cpu.tile_map.tiles, cpu.tile_images)
        ],
        content_id="p_gpu",
        time_base="1/30",
        grid_cols=2,
    )
    assert not compare_measure_results(cpu, far, tolerance=0.02)[
        "within_tolerance"
    ]


def test_measure_with_fallback() -> None:
    tiles = [
        TileInput(t=_rt("0"), pts=0, frame_no=0, image_bytes=_image(0)),
        TileInput(t=_rt("1"), pts=30, frame_no=30, image_bytes=_image(20)),
    ]
    result, fallback = measure_with_fallback(
        _FailingGpu(), tiles, content_id="p_gpu", time_base="1/30"
    )
    assert fallback is True
    assert result.measurements


def test_effective_fps_uses_gpu_only_when_available() -> None:
    settings = Settings(
        workspace_root=".",
        gpu_measure_enabled=True,
        gpu_measure_fps=12.0,
        view_fps=2.0,
    )
    assert effective_measure_fps(settings, CpuViewMeasurer()) == 2.0

    class _GpuProbe:
        backend = "cuda"
        accelerated = True

    assert effective_measure_fps(settings, _GpuProbe()) == 12.0

    class _PlaceholderProbe:
        backend = "cuda-placeholder"
        implementation = "cuda-placeholder"
        accelerated = False

    # 占位实现只能增加抽帧，不能提升采样率。
    assert effective_measure_fps(settings, _PlaceholderProbe()) == 2.0
    assert CudaViewMeasurerPlaceholder.accelerated is False
    assert CudaViewMeasurerPlaceholder.backend == "cuda-placeholder"
    disabled = settings.model_copy(update={"gpu_measure_enabled": False})
    assert effective_measure_fps(disabled, _GpuProbe()) == 2.0
    assert build_view_measurer(disabled).backend == "cpu"


def test_measure_backend_is_excluded_from_render_hash() -> None:
    common = dict(
        schema_version="1.0",
        kind="contact_sheet",
        media_sha256_16="a" * 16,
        tile_map_sha="b" * 64,
        measurements_sha="c" * 64,
        image_bytes_sha="d" * 64,
    )
    meta = {
        "contract_version": "1.0",
        "policy_version": "views@1.0",
        "ffmpeg_version": "7",
        "opencv_version": "4.14",
        "fingerprint_version": "dhash:v2",
        "fingerprint_engine": "cv2",
        "motion_split_version": "affine@1",
        "measure_backend": "cpu",
        "fps_sample": 2.0,
        "dedup_threshold": 4,
        "scene_threshold_pct": 0.0,
        "color_convention": "bt709/limited",
        "yuv_to_rgb_matrix_id": "bt709",
        "tile_label_format": "T%02d",
        "grid_rows": 1,
        "grid_cols": 2,
        "tile_w": 320,
        "tile_h": 180,
        "pad_px": 4,
        "shader_hash": "cpu:pillow/1",
        "backend": "cpu",
        "device_tier": "cpu",
    }
    gpu_meta = {**meta, "measure_backend": "cuda", "backend": "qtgl"}
    assert compute_render_hash(meta=meta, **common) == compute_render_hash(
        meta=gpu_meta, **common
    )


def test_service_falls_back_to_cpu_measure(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    settings = settings.model_copy(
        update={"vision_use_evidence": True, "view_evidence_enabled": True}
    )
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(
        settings,
        db,
        jobs,
        transcriber=NullTranscriber(),
        view_measurer=_FailingGpu(),
    )
    try:
        project_id = services.create_project("gpu-fallback")["id"]
        services.ingest(project_id, str(synthetic_video))
        services.extract_frames(project_id)
        result = services.build_visual_evidence(project_id)
        assert result["available"] is True
        assert result["metrics"]["measure_backend"] == "cpu_fallback"
        assert isinstance(services.view_measurer, CpuViewMeasurer)
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
