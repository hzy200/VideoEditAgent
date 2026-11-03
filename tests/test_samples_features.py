"""samples/ 真实素材的功能覆盖测试（感知、AI mock、渲染、音频、转码、运维）。

显式运行：
    python -m pytest -q -m samples tests/test_samples_features.py
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from smartcut.config import Settings
from smartcut.contracts.audio import AudioTrackSpec
from smartcut.contracts.captions import CaptionCue
from smartcut.contracts.effects import ColorEffect, FitEffect
from smartcut.contracts.edit_ir import (
    AudioSpec,
    EditIR,
    ExportSpec,
    TimeRange,
    TrimOp,
    trim_id_for,
)
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.time import RationalTime
from smartcut.contracts.templates import EditTemplate
from smartcut.contracts.timeline_ops import (
    OverlaySpec,
    WatermarkRemovalSpec,
)
from smartcut.contracts.transcode import TranscodeProfile, TranscodeRequest
from smartcut.perception.transcribe import NullTranscriber
from smartcut.planner.client import DeepSeekPlanner
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
SHORT = SAMPLES / "任然MV _ 我的《飞鸟和蝉》，纪念结束的夏天.mp4"
LONG = SAMPLES / "[1080P超清修复版]铠甲勇士 01-52全集_P1_01.mp4"
SPEECH = SAMPLES / "李世民对长孙无忌是真爱啊.mp4"
AUDIO = [
    SAMPLES / "res.mp3",
    SAMPLES / "宮崎慎二 - 戦闘！四天王_H.ogg",
    SAMPLES / "青衣.mp3",
]

pytestmark = [
    pytest.mark.samples,
    pytest.mark.skipif(
        not SAMPLES.is_dir(),
        reason="samples/ 不存在；跳过真实素材功能测试",
    ),
]


def _rt(seconds: float) -> RationalTime:
    return RationalTime.from_seconds(round(float(seconds), 3), rate=90_000)


def _require(*paths: Path) -> None:
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        pytest.skip(f"缺少样本: {missing}")


@pytest.fixture(scope="session")
def sample_settings(tmp_path_factory, ffmpeg_bin, ffprobe_bin) -> Settings:
    root = tmp_path_factory.mktemp("samples-features")
    return Settings(
        workspace_root=root,
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
        max_workers=2,
        deepseek_api_key=None,
        hardware_encoding="off",
        hardware_decoding="off",
        whisper_model_path=str(
            SAMPLES.parent / "vendor" / "whisper" / "base"
        ),
    )


@pytest.fixture(scope="session")
def sample_services(sample_settings):
    db = Database(sample_settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(
        sample_settings, db, jobs, transcriber=NullTranscriber()
    )
    try:
        yield services
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


@pytest.fixture(scope="session")
def short_project(sample_services):
    _require(SHORT)
    project = sample_services.create_project("sample-short")
    ingested = sample_services.ingest(project["id"], str(SHORT))
    return {
        "id": project["id"],
        "src": ingested["src"],
        "facts": MediaFacts.model_validate(ingested["facts"]),
    }


@pytest.fixture(scope="session")
def perceived_short(sample_services, short_project):
    project_id = short_project["id"]
    frames = sample_services.extract_frames(project_id)
    scenes = sample_services.detect_scenes(project_id)
    quality = sample_services.analyze_quality(project_id)
    faces = sample_services.detect_faces(project_id)
    sheet = sample_services.build_contact_sheet(project_id)
    chapters = sample_services.build_chapters(project_id)
    candidates = sample_services.build_candidates(project_id)
    return {
        "frames": frames,
        "scenes": scenes,
        "quality": quality,
        "faces": faces,
        "sheet": sheet,
        "chapters": chapters,
        "candidates": candidates,
    }


@pytest.fixture(scope="session")
def audio_assets(sample_services, short_project):
    _require(*AUDIO)
    return [
        sample_services.import_audio_asset(
            short_project["id"], str(path), name=f"sample-audio-{index}"
        )
        for index, path in enumerate(AUDIO)
    ]


def _one_trim_ir(
    project_id: str,
    src: str,
    facts: MediaFacts,
    *,
    start: float = 0.0,
    end: float = 5.0,
    **kwargs,
) -> EditIR:
    return EditIR(
        project_id=project_id,
        media_facts=facts,
        sequence=[
            TrimOp(
                id=trim_id_for(
                    src, TimeRange(start=_rt(start), end=_rt(end))
                ),
                src=src,
                range=TimeRange(start=_rt(start), end=_rt(end)),
            )
        ],
        **kwargs,
    )


def test_proxy_waveforms_and_audio_assets(
    sample_services, short_project, audio_assets
) -> None:
    project_id = short_project["id"]
    proxy = sample_services.create_proxy(project_id)
    assert proxy["proxy"]

    waveform = sample_services.build_waveform(project_id)
    assert waveform.get("peaks")
    assert len(waveform["peaks"]) > 64

    audio_waveforms = sample_services.build_audio_waveforms(project_id)
    assert audio_waveforms.get("main")
    assert len(audio_waveforms.get("assets") or {}) == len(audio_assets)


def test_perception_pipeline(perceived_short) -> None:
    assert perceived_short["frames"]["available"]
    assert perceived_short["frames"]["count"] > 0
    assert perceived_short["scenes"]["available"]
    assert perceived_short["scenes"]["shot_count"] > 0
    assert perceived_short["quality"]["available"]
    assert "marks" in perceived_short["quality"]["report"]
    assert perceived_short["faces"]["available"]
    assert "anchors" in perceived_short["faces"]["report"]
    assert perceived_short["sheet"]["path"]
    assert perceived_short["chapters"]["available"]
    assert perceived_short["chapters"]["chapters"]
    assert perceived_short["candidates"]["available"]
    assert perceived_short["candidates"]["candidate_count"] > 0


def test_perception_cache_hits(sample_services, short_project) -> None:
    project_id = short_project["id"]
    assert sample_services.extract_frames(project_id)["cached"] is True
    assert sample_services.detect_scenes(project_id)["cached"] is True
    assert sample_services.analyze_quality(project_id)["cached"] is True
    assert sample_services.detect_faces(project_id)["cached"] is True
    assert sample_services.build_candidates(project_id)["cached"] is True


def test_manifest_memory_and_candidate_retrieval(
    sample_services, short_project, perceived_short
) -> None:
    project_id = short_project["id"]
    manifest = sample_services.build_video_manifest(project_id)["manifest"]
    memory = sample_services.build_video_memory(project_id)["memory"]
    assert manifest["chapters"]["chapters"]
    assert memory["chapters"]
    assert manifest["candidates"]["count"] > 0

    candidates = sample_services.get_candidates(project_id)["candidates"]
    query = (candidates[0].get("tags") or ["candidate"])[0]
    retrieved = sample_services.retrieve_candidates(project_id, query)
    assert retrieved["candidates"]


def test_render_fit_captions_loudness(sample_services, short_project) -> None:
    project_id = short_project["id"]
    ir = _one_trim_ir(
        project_id,
        short_project["src"],
        short_project["facts"],
        effects=[FitEffect(anchor="center")],
        subtitles=[
            CaptionCue(
                start=_rt(0),
                end=_rt(2),
                text="真实素材字幕",
            )
        ],
        audio=AudioSpec(
            loudness="-14 LUFS",
            volume_db=-2.0,
            pitch_semitones=1.0,
        ),
        export=ExportSpec(burn_captions=True),
    )
    revision = sample_services.submit_revision(project_id, ir)["revision"]
    result = sample_services.render(project_id, revision)
    output = sample_services.probe(project_id, result["output_path"])
    assert output.width == 1080
    assert output.height == 1920
    assert output.has_audio
    assert abs(output.duration.to_float() - 5.0) <= 0.2
    workspace = sample_services.project_workspace(project_id)
    assert (workspace.resolve(result["output_path"])).exists()
    assert result["caption_srt_path"]
    assert (workspace.resolve(result["caption_srt_path"])).exists()


def test_image_logo_watermark_and_delogo_render(
    sample_services, short_project, tmp_path
) -> None:
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    logo = tmp_path / "logo.png"
    image = Image.new("RGBA", (160, 60), (220, 30, 30, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((6, 6, 154, 54), outline=(255, 255, 255, 255), width=3)
    image.save(logo)

    project_id = short_project["id"]
    asset = sample_services.import_overlay_asset(
        project_id, str(logo), name="logo"
    )
    assert asset["width"] == 160
    assert asset["height"] == 60
    assert any(
        item["id"] == asset["id"]
        for item in sample_services.list_overlay_assets(project_id)
    )

    ir = _one_trim_ir(
        project_id,
        short_project["src"],
        short_project["facts"],
        overlays=[
            OverlaySpec(
                kind="image",
                asset_id=asset["id"],
                scale=0.2,
                position="bottom_right",
            )
        ],
        removals=[
            WatermarkRemovalSpec(
                id="rm_sample",
                x=10,
                y=10,
                width=100,
                height=50,
                start=_rt(0.5),
                end=_rt(2.0),
            )
        ],
    )
    revision = sample_services.submit_revision(project_id, ir)["revision"]
    plan = sample_services.dry_run(project_id, revision)
    post = next(
        call for call in plan["tool_calls"] if call["tool"] == "postprocess"
    )
    assert post["args"]["overlays"][0]["kind"] == "image"
    assert post["args"]["overlays"][0]["src"].startswith("overlays/")
    assert post["args"]["removals"]

    result = sample_services.render(project_id, revision)
    output = sample_services.probe(project_id, result["output_path"])
    assert output.has_audio
    assert abs(output.duration.to_float() - 5.0) <= 0.2


def test_render_multitrack_audio(
    sample_services, short_project, audio_assets
) -> None:
    project_id = short_project["id"]
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
        for index, asset in enumerate(audio_assets)
    ]
    ir = _one_trim_ir(
        project_id,
        short_project["src"],
        short_project["facts"],
        audio=AudioSpec(tracks=tracks),
    )
    revision = sample_services.submit_revision(project_id, ir)["revision"]
    result = sample_services.render(project_id, revision)
    output = sample_services.probe(project_id, result["output_path"])
    assert output.has_audio
    assert abs(output.duration.to_float() - 5.0) <= 0.2


def test_render_copy_mode(sample_services, short_project) -> None:
    project_id = short_project["id"]
    ir = _one_trim_ir(
        project_id,
        short_project["src"],
        short_project["facts"],
        end=3.0,
        export=ExportSpec(cut_mode="copy"),
    )
    revision = sample_services.submit_revision(project_id, ir)["revision"]
    result = sample_services.render(project_id, revision)
    output = sample_services.probe(project_id, result["output_path"])
    assert output.has_audio
    assert abs(output.duration.to_float() - 3.0) <= 0.2


def test_transcode_profiles(sample_services, short_project) -> None:
    project_id = short_project["id"]
    reencoded = sample_services.transcode(
        project_id,
        TranscodeRequest(
            profile=TranscodeProfile(
                container="mp4",
                video_codec="libx264",
                width=640,
                height=360,
                preset="veryfast",
                crf=28,
            )
        ),
    )
    assert Path(reencoded["output_abs"]).exists()
    assert reencoded["width"] == 640
    assert reencoded["height"] == 360

    copied = sample_services.transcode(
        project_id,
        TranscodeRequest(
            profile=TranscodeProfile(
                container="mkv",
                video_codec="copy",
                fps_mode="passthrough",
            )
        ),
    )
    assert Path(copied["output_abs"]).exists()


def test_effects_preview_and_caption_preview(
    sample_services, short_project
) -> None:
    project_id = short_project["id"]
    ir = _one_trim_ir(
        project_id,
        short_project["src"],
        short_project["facts"],
        effects=[ColorEffect(brightness=0.15)],
        subtitles=[
            CaptionCue(start=_rt(0), end=_rt(3), text="预览字幕")
        ],
    )
    preview = sample_services.render_effects_preview(
        project_id, ir, max_height=240
    )
    assert preview.get("path")
    assert Path(preview["path"]).exists()

    revision = sample_services.submit_revision(project_id, ir)["revision"]
    captions = sample_services.caption_preview(project_id, revision)
    assert captions["available"]
    assert "预览字幕" in captions["srt"]


def test_render_review_rules(sample_services, short_project) -> None:
    project_id = short_project["id"]
    ir = _one_trim_ir(
        project_id,
        short_project["src"],
        short_project["facts"],
    )
    revision = sample_services.submit_revision(project_id, ir)["revision"]
    sample_services.render(project_id, revision)
    review = sample_services.review_render(
        project_id, revision, goal="检查成片"
    )
    assert review["revision"] == revision
    assert review["score"] > 0
    assert "duration" in review["checks"]


def test_cache_reuses_rendered_segments(sample_services, short_project) -> None:
    project_id = short_project["id"]
    ir = _one_trim_ir(
        project_id,
        short_project["src"],
        short_project["facts"],
        end=3.0,
    )
    revision = sample_services.submit_revision(project_id, ir)["revision"]
    first = sample_services.render(project_id, revision)
    second = sample_services.render(project_id, revision)
    assert second["cache_hits"] >= first["cache_hits"] + 1


def test_template_crud(sample_services) -> None:
    template = EditTemplate(
        name="sample-template",
        goal="sample goal",
        allow_render=False,
        phase="outline",
    )
    created = sample_services.create_template(template)
    listed = sample_services.list_templates()
    assert any(item["id"] == created["id"] for item in listed)
    sample_services.delete_template(created["id"])
    assert all(
        item["id"] != created["id"]
        for item in sample_services.list_templates()
    )


def test_user_preference_and_revision_diff(
    sample_services, short_project
) -> None:
    project_id = short_project["id"]
    first = _one_trim_ir(
        project_id,
        short_project["src"],
        short_project["facts"],
        end=4.0,
    )
    rev1 = sample_services.submit_revision(
        project_id, first, origin="user"
    )["revision"]
    second = _one_trim_ir(
        project_id,
        short_project["src"],
        short_project["facts"],
        start=1.0,
        end=6.0,
    )
    rev2 = sample_services.submit_revision(
        project_id, second, origin="user"
    )["revision"]

    preference = sample_services.get_edit_preference(project_id)
    assert preference is not None
    assert preference["stats"]["sample_size"] >= 2

    diff = sample_services.revision_diff(project_id, rev2, rev1)
    assert diff["added_count"] + diff["removed_count"] > 0
    assert "effects_changed" in diff


def test_project_admin_rename_delete(sample_services) -> None:
    _require(SPEECH)
    project = sample_services.create_project("sample-admin")
    sample_services.ingest(project["id"], str(SPEECH))
    renamed = sample_services.rename_project(project["id"], "改名后的样本")
    assert renamed["name"] == "改名后的样本"
    workspace = sample_services.project_workspace(project["id"])
    deleted = sample_services.delete_project(project["id"])
    assert deleted["deleted"] == project["id"]
    assert not workspace.root.exists()


def _mock_response(content: str) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"content": content}}]}
    )


def _mock_draft(src: str, start: float = 0.0, end: float = 5.0) -> str:
    return json.dumps(
        {
            "sequence": [
                {
                    "op": "trim",
                    "src": src,
                    "range": {
                        "start": _rt(start).model_dump(),
                        "end": _rt(end).model_dump(),
                    },
                    "tag": "mock",
                    "confidence": 0.8,
                }
            ],
            "effects": [],
            "audio": {
                "loudness": None,
                "remove_filler": False,
                "bgm": None,
            },
            "export": {
                "preset": "mp4_h264",
                "burn_captions": False,
                "cut_mode": "reencode",
            },
        },
        ensure_ascii=False,
    )


def _mock_handler(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    system = payload["messages"][0]["content"]
    user: dict = {}
    for message in reversed(payload["messages"]):
        content = str(message.get("content") or "")
        if not content.strip().startswith("{"):
            continue
        try:
            user = json.loads(content)
            break
        except json.JSONDecodeError:
            continue
    src = (user.get("media_facts") or {}).get("src") or "media/source.mp4"
    if "重规划器" in system:
        return _mock_response(_mock_draft(src))
    if "审片器" in system:
        return _mock_response(
            json.dumps(
                {"score": 0.92, "summary": "mock review", "issues": []},
                ensure_ascii=False,
            )
        )
    if "章节选择器" in system:
        chapters = (user.get("chapters") or {}).get("chapters") or []
        ids = [chapters[0]["id"]] if chapters else []
        return _mock_response(
            json.dumps({"chapter_ids": ids, "rationale": "mock"})
        )
    return _mock_response(_mock_draft(src))


def _with_mock_planner(services: Services, settings: Settings):
    mock_settings = settings.model_copy(
        update={"deepseek_api_key": "test"}
    )
    planner = DeepSeekPlanner(
        mock_settings, transport=httpx.MockTransport(_mock_handler)
    )
    original = services.planner
    services.planner = planner
    return planner, original


def test_ai_plan_replan_review_with_mock(
    sample_services, sample_settings, short_project
) -> None:
    planner, original = _with_mock_planner(sample_services, sample_settings)
    try:
        result = sample_services.plan(
            short_project["id"],
            "mock goal",
            perception={},
            chapters={"chapters": []},
            candidates={"candidates": []},
        )
        assert result["revision"] >= 1
        revision = sample_services.get_revision(
            short_project["id"], result["revision"]
        )
        assert revision.sequence

        replanned = sample_services.replan(
            short_project["id"],
            result["revision"],
            "mock goal",
            {"score": 0.5, "issues": [{"severity": "warning", "message": "test"}]},
        )
        assert replanned["revision"] > result["revision"]

        review = sample_services.review_edit(
            short_project["id"], result["revision"], "mock goal"
        )
        assert review["score"] == pytest.approx(0.92)
        assert review["source"] == "llm"
    finally:
        sample_services.planner = original
        planner.close()


def test_ai_pinned_effects_with_mock(
    sample_services, sample_settings, short_project
) -> None:
    planner, original = _with_mock_planner(sample_services, sample_settings)
    try:
        result = sample_services.plan(
            short_project["id"],
            "mock pinned",
            perception={},
            chapters={"chapters": []},
            candidates={"candidates": []},
            pinned_effects=[ColorEffect(brightness=0.2)],
        )
        revision = sample_services.get_revision(
            short_project["id"], result["revision"]
        )
        assert revision.effects
        assert revision.effects[0].type == "color"
    finally:
        sample_services.planner = original
        planner.close()


def test_vlm_disabled_degrades(
    sample_services, sample_settings, short_project
) -> None:
    assert sample_settings.vision_configured is False
    assert sample_services.vision.available is False
    notes = sample_services.build_visual_notes(
        short_project["id"], "goal", {"chapters": []}
    )
    assert notes is None


def test_local_asr_on_short_speech_clip(
    tmp_path, sample_settings, ffmpeg_bin
) -> None:
    pytest.importorskip("faster_whisper")
    _require(SPEECH)
    model_path = Path(sample_settings.whisper_model_path or "")
    if not model_path.exists():
        pytest.skip("缺少本地 whisper 模型")

    clip = tmp_path / "speech_30s.mp4"
    subprocess.run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-ss",
            "10",
            "-t",
            "30",
            "-i",
            str(SPEECH),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(clip),
        ],
        check=True,
        capture_output=True,
    )
    asr_settings = sample_settings.model_copy(
        update={
            "workspace_root": tmp_path / "asr-workspace",
            "whisper_device": "cpu",
            "whisper_compute_type": "int8",
            "deepseek_api_key": None,
        }
    )
    db = Database(asr_settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(asr_settings, db, jobs)
    try:
        project = services.create_project("sample-asr")
        services.ingest(project["id"], str(clip))
        result = services.transcribe(project["id"])
        transcript = services.get_transcript(project["id"])
        assert result["degraded"] is False
        assert transcript is not None
        assert not transcript.degraded
        assert transcript.segments or transcript.words
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
