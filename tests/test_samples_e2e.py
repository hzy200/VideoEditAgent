"""基于 samples/ 真实素材的集成测试。

默认不进入快速回归；显式运行：
    python -m pytest -q -m samples tests/test_samples_e2e.py
"""

from __future__ import annotations

import re
import shutil
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import pytest

from smartcut.config import Settings
from smartcut.contracts.audio import AudioTrackSpec
from smartcut.contracts.effects import BlurEffect, ColorEffect
from smartcut.contracts.edit_ir import (
    AudioSpec,
    EditIR,
    TimeRange,
    TrimOp,
    trim_id_for,
)
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.time import RationalTime
from smartcut.contracts.timeline_ops import OverlaySpec, TransitionSpec
from smartcut.contracts.tools import ToolCall
from smartcut.perception.chunking import split_wav
from smartcut.perception.transcribe import NullTranscriber
from smartcut.runner.execute import ToolCallExecutor
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
SHORT_VIDEOS = [
    "万能指挥家 Tom and Jerry in the Hollywood Bowl（猫和老鼠）.mp4",
    "任然MV _ 我的《飞鸟和蝉》，纪念结束的夏天.mp4",
    "李世民对长孙无忌是真爱啊.mp4",
]
LONG_VIDEO = "[1080P超清修复版]铠甲勇士 01-52全集_P1_01.mp4"
AUDIO_FILES = [
    "res.mp3",
    "宮崎慎二 - 戦闘！四天王_H.ogg",
    "青衣.mp3",
]

pytestmark = [
    pytest.mark.samples,
    pytest.mark.skipif(
        not SAMPLES.is_dir(),
        reason="samples/ 不存在；跳过真实素材集成测试",
    ),
]


def _rt(seconds: float) -> RationalTime:
    return RationalTime.from_seconds(round(float(seconds), 3), rate=90_000)


@contextmanager
def _services(settings: Settings):
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        yield services
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def _require(*names: str) -> list[Path]:
    paths = [SAMPLES / name for name in names]
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        pytest.skip(f"缺少样本: {missing}")
    return paths


def _ingest(services: Services, path: Path, name: str) -> tuple[dict, MediaFacts]:
    project = services.create_project(name)
    ingested = services.ingest(project["id"], str(path))
    facts = MediaFacts.model_validate(ingested["facts"])
    return project, facts


def test_short_videos_ingest_and_probe(settings) -> None:
    paths = _require(*SHORT_VIDEOS)
    with _services(settings) as services:
        for index, path in enumerate(paths):
            project, facts = _ingest(services, path, f"short-{index}")
            assert facts.video is not None
            assert facts.width > 0 and facts.height > 0
            assert facts.has_audio
            assert facts.duration.to_float() > 60
            workspace = services.project_workspace(project["id"])
            ingested_src = services.get_project(project["id"])["asset"]["src"]
            assert workspace.resolve(ingested_src).exists()
            assert re.fullmatch(
                r"[A-Za-z0-9._-]+", PurePosixPath(ingested_src).name
            )
            # 源文件只读：ingest 只复制，不得改动 samples 原文件。
            assert path.is_file()


def test_short_video_effects_transition_speed_watermark_render(settings) -> None:
    path = _require(SHORT_VIDEOS[1])[0]  # 25fps CFR，适合确定性渲染
    with _services(settings) as services:
        project, facts = _ingest(services, path, "sample-render")
        project_id = project["id"]
        src = services.get_project(project_id)["asset"]["src"]

        first_range = TimeRange(start=_rt(0), end=_rt(3))
        second_range = TimeRange(start=_rt(5), end=_rt(8))
        first_id = trim_id_for(src, first_range)
        second_id = trim_id_for(src, second_range)
        ir = EditIR(
            project_id=project_id,
            media_facts=facts,
            sequence=[
                TrimOp(
                    id=first_id,
                    src=src,
                    range=first_range,
                    speed=1.5,
                    tag="fast",
                ),
                TrimOp(
                    id=second_id,
                    src=src,
                    range=second_range,
                    tag="normal",
                ),
            ],
            effects=[
                ColorEffect(brightness=0.1),
                BlurEffect(radius=2.0, target="segment", segment_id=first_id),
            ],
            transitions=[
                TransitionSpec(after=first_id, duration=_rt(0.5))
            ],
            overlays=[
                OverlaySpec(text="SmartCut", position="bottom_right")
            ],
        )
        submitted = services.submit_revision(project_id, ir)
        result = services.render(project_id, submitted["revision"])
        output = services.probe(project_id, result["output_path"])

        expected_seconds = 3 / 1.5 + 3 - 0.5
        assert abs(output.duration.to_float() - expected_seconds) <= 0.2
        assert output.width == facts.width
        assert output.height == facts.height
        assert output.has_audio
        workspace = services.project_workspace(project_id)
        assert workspace.resolve(result["output_path"]).exists()


def test_audio_assets_import_and_mix_preview(settings) -> None:
    video_path = _require(SHORT_VIDEOS[1])[0]
    audio_paths = _require(*AUDIO_FILES)
    with _services(settings) as services:
        project, facts = _ingest(services, video_path, "sample-audio")
        project_id = project["id"]
        src = services.get_project(project_id)["asset"]["src"]
        assets = [
            services.import_audio_asset(
                project_id, str(path), name=f"sample-{index}"
            )
            for index, path in enumerate(audio_paths)
        ]
        tracks = [
            AudioTrackSpec(
                id=f"t{index}",
                role="music" if index < 2 else "other",
                asset_id=asset["id"],
                source_start=_rt(0),
                source_end=_rt(5),
                timeline_start=_rt(0),
                gain_db=-6.0 - index * 3.0,
            )
            for index, asset in enumerate(assets)
        ]
        ir = EditIR(
            project_id=project_id,
            media_facts=facts,
            sequence=[
                TrimOp(
                    src=src,
                    range=TimeRange(start=_rt(0), end=_rt(5)),
                )
            ],
            audio=AudioSpec(tracks=tracks),
        )
        result = services.build_preview_mix(project_id, ir)
        workspace = services.project_workspace(project_id)
        relative = Path(result["path"]).relative_to(workspace.root).as_posix()
        mixed = services.probe(project_id, relative)
        assert mixed.has_audio
        assert abs(mixed.duration.to_float() - 5.0) <= 0.3


@pytest.mark.skipif(
    not (SAMPLES / LONG_VIDEO).is_file(), reason="缺少长视频样本"
)
def test_long_video_fast_outline(settings) -> None:
    pytest.importorskip("scenedetect")
    path = SAMPLES / LONG_VIDEO
    with _services(settings) as services:
        project, facts = _ingest(services, path, "sample-long")
        assert facts.duration.to_float() > 1200
        run = services.create_orchestration_run(
            project["id"],
            "总结主线",
            allow_render=False,
            phase="fast_outline",
        )
        services.orchestrate_run(run["id"])
        stored = services.get_orchestration_run(run["id"])
        assert stored["status"] in {"succeeded", "awaiting_approval"}
        chapters = services.get_chapters(project["id"])
        assert chapters and chapters.get("chapters")
        manifest = services.build_video_manifest(project["id"])["manifest"]
        memory = services.build_video_memory(project["id"])["memory"]
        assert manifest["chapters"]["chapters"]
        assert memory["chapters"]


def test_long_audio_chunking(settings) -> None:
    path = _require(AUDIO_FILES[2])[0]
    with _services(settings) as services:
        project = services.create_project("sample-long-audio")
        project_id = project["id"]
        asset = services.import_audio_asset(project_id, str(path), name="青衣")
        workspace = services.project_workspace(project_id)
        executor = ToolCallExecutor(settings, workspace)
        executor.execute(
            ToolCall(
                seq=0,
                tool="extract_audio",
                args={
                    "src": asset["src"],
                    "dst": "cache/audio/sample_long.wav",
                },
            )
        )
        wav = workspace.resolve("cache/audio/sample_long.wav")
        chunks = split_wav(
            wav,
            workspace.tmp_dir / "sample_chunks",
            chunk_seconds=300,
            overlap_seconds=1.0,
        )
        assert len(chunks) >= 12
        assert chunks[0].start == pytest.approx(0.0)
        assert chunks[-1].end > 3600
