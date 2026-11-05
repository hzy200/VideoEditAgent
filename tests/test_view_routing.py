from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

from smartcut.config import Settings
from smartcut.contracts.time import RationalTime, seconds_from_mapping
from smartcut.contracts.vision import ImageVisualNote, ImageVisualNotes
from smartcut.perception.transcribe import NullTranscriber
from smartcut.perception.view.route import (
    RouteRule,
    RoutingRules,
    route_views,
    rules_from_policy,
)
from smartcut.planner.vision import select_evidence_images
from smartcut.policy import load_policy
from smartcut.runner.paths import Workspace
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services
from smartcut.server.services.visual import VisualMixin


def _rules() -> RoutingRules:
    rules = rules_from_policy(load_policy().vision.routing)
    assert rules is not None
    return rules


def test_route_views_uses_goal_keywords() -> None:
    rules = _rules()

    talking = route_views("做一期口播访谈", {}, None, 60.0, rules)
    assert talking.content_type == "talking_head"
    assert talking.primary == "bbox_overlay"
    assert talking.extras == ("roi_crop",)

    motion = route_views("剪一段运动比赛集锦", {}, None, 60.0, rules)
    assert motion.content_type == "motion_demo"
    assert motion.primary == "temporal_diff"
    assert motion.extras == ("flow_strip",)

    screen = route_views("录屏教程切片", {}, None, 60.0, rules)
    assert screen.content_type == "screen_content"
    assert screen.primary == "contact_sheet"
    assert screen.extras == ("roi_crop",)

    codec = route_views("检查码率与压缩痕迹", {}, None, 60.0, rules)
    assert codec.content_type == "compression_suspect"
    assert codec.primary == "contact_sheet"
    assert codec.extras == ("codec_residual",)


def test_route_views_uses_face_and_shot_signals() -> None:
    rules = _rules()
    perception = {
        "frame_manifest": [{"index": index} for index in range(100)],
        "faces": {"anchors": [{"x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2}] * 50},
        "shots": {"shots": []},
    }
    route = route_views("", perception, None, 60.0, rules)
    assert route.content_type == "talking_head"
    assert route.primary == "bbox_overlay"

    fast = {
        "frame_manifest": [{"index": index} for index in range(100)],
        "faces": {"anchors": []},
        "shots": {"shots": [{"t": {"value": index * 90_000, "rate": 90_000}} for index in range(60)]},
    }
    route = route_views("", fast, None, 60.0, rules)
    assert route.content_type == "motion_demo"
    assert route.primary == "temporal_diff"


def test_route_views_falls_back_without_policy() -> None:
    route = route_views("任意目标", {}, None, 10.0, None)
    assert route.primary == "contact_sheet"
    assert route.extras == ()
    assert route.content_type == "unrouted"


def test_route_equivalence_is_deterministic() -> None:
    rules = RoutingRules(
        version="route@test",
        default="generic",
        types=(
            RouteRule("generic", "contact_sheet"),
            RouteRule("motion_demo", "temporal_diff", ("flow_strip",)),
        ),
    )
    first = route_views("运动演示", {}, None, 30.0, rules)
    second = route_views("运动演示", {}, None, 30.0, rules)
    assert first == second


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        workspace_root=tmp_path / "workspace",
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        deepseek_api_key=None,
        hardware_encoding="off",
        vision_base_url="https://vlm.example/v1",
        vision_model="vlm-test",
        vision_api_key="vision-key",
        vision_max_images=3,
        vision_image_max_edge=128,
        vision_image_max_bytes=200_000,
        vision_total_max_bytes=2_000_000,
    )
    values.update(overrides)
    return Settings(**values)


def _evidence_view(
    workspace: Workspace,
    name: str,
    *,
    kind: str,
    evidence_id: str,
    labels: list[str],
    color: tuple[int, int, int] = (20, 30, 40),
    measurements: list[dict] | None = None,
) -> dict:
    relative = f"cache/{name}.jpg"
    path = workspace.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 36), color).save(path, format="JPEG")
    return {
        "available": True,
        "kind": kind,
        "image_ref": relative,
        "evidence": {
            "id": evidence_id,
            "kind": kind,
            "measurements": measurements or [],
            "tile_map": {
                "tiles": [
                    {
                        "label": label,
                        "t": {"value": index * 90_000, "rate": 90_000},
                        "pts": index * 3_000,
                        "frame_no": index,
                    }
                    for index, label in enumerate(labels)
                ]
            },
        },
    }


def test_select_evidence_images_accepts_multiple_views(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.root.mkdir(parents=True, exist_ok=True)
    first = _evidence_view(
        workspace,
        "first",
        kind="contact_sheet",
        evidence_id="ve_first",
        labels=["T01", "T02"],
        measurements=[{"d_hist": 1.0, "width": 64, "height": 36, "score": 0.5}],
    )
    second = _evidence_view(
        workspace,
        "second",
        kind="roi_crop",
        evidence_id="ve_second",
        labels=["T03"],
        color=(200, 120, 40),
        measurements=[
            {
                "d_hist": 0.42,
                "flow_mag": 2.1,
                "flow_dir_deg": 123.4,
                "bbox_xywh": [10, 10, 40, 18],
                "width": 64,
                "height": 36,
                "score": 0.7,
            }
        ],
    )

    images = select_evidence_images(
        workspace,
        [first, second],
        _settings(tmp_path),
        perception={
            "quality": {
                "marks": [
                    {"t": {"value": 0, "rate": 90_000}, "kind": "blurry"},
                ]
            }
        },
    )
    assert len(images) == 2
    assert [image.evidence_id for image in images] == ["ve_first", "ve_second"]
    assert images[0].tile_metadata[0]["label"] == "T01"
    assert images[1].tile_metadata[0]["label"] == "T03"
    assert images[0].sha256 != images[1].sha256
    facts = images[1].tile_metadata[0]
    assert facts["d_hist"] == 0.42
    assert facts["flow_mag"] == 2.1
    assert facts["flow_dir_deg"] == 123
    assert facts["face"] == 0.31
    assert facts["score"] == 0.7
    assert facts["risk"] == "blurry"
    # 首帧 d_hist=1.0 是 measure 的占位值，不应进入 prompt。
    assert "d_hist" not in images[0].tile_metadata[0]
    assert "flow_mag" not in images[0].tile_metadata[0]


def test_select_evidence_images_respects_global_budget(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.root.mkdir(parents=True, exist_ok=True)
    views = [
        _evidence_view(
            workspace,
            f"view-{index}",
            kind="contact_sheet",
            evidence_id=f"ve_{index}",
            labels=[f"T{index + 1:02d}"],
        )
        for index in range(3)
    ]
    images = select_evidence_images(
        workspace,
        views,
        _settings(tmp_path, vision_max_images=1),
    )
    assert [image.evidence_id for image in images] == ["ve_0"]


class _FakeVision:
    available = True
    model = "fake-vlm"

    def __init__(self) -> None:
        self.message_batches: list[list[dict]] = []

    def analyze(self, messages, schema):
        assert schema is ImageVisualNotes
        self.message_batches.append(messages)
        count = sum(
            1 for part in messages[1]["content"] if part.get("type") == "image_url"
        )
        return ImageVisualNotes(
            images=[
                ImageVisualNote(image_index=index, summary=f"画面 {index}")
                for index in range(count)
            ]
        )

    def close(self) -> None:
        pass


def test_service_routes_views_and_keeps_labels_disjoint(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    settings = settings.model_copy(
        update={"vision_use_evidence": True, "view_evidence_enabled": True}
    )
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    fake = _FakeVision()
    services.vision = fake
    try:
        project_id = services.create_project("routing")["id"]
        services.ingest(project_id, str(synthetic_video))
        services.extract_frames(project_id)
        services.build_chapters(project_id)
        chapters = services.get_chapters(project_id)
        assert chapters is not None

        first = services.build_visual_evidence(
            project_id,
            kind="contact_sheet",
            label_start=1,
        )
        first_labels = [
            tile["label"]
            for tile in first["evidence"]["tile_map"]["tiles"]
        ]
        second = services.build_visual_evidence(
            project_id,
            kind="contact_sheet",
            label_start=1 + len(first_labels),
        )
        second_labels = [
            tile["label"]
            for tile in second["evidence"]["tile_map"]["tiles"]
        ]
        assert first_labels and second_labels
        assert set(first_labels).isdisjoint(second_labels)
        assert second_labels[0] == f"T{1 + len(first_labels):02d}"

        result = services.build_visual_notes(project_id, "口播讲解", chapters)
        assert result is not None
        assert result["evidence_ids"]
        assert result["view_kinds"] == ["bbox_overlay", "roi_crop"]
        assert result["visual_facts"]
        fact = result["visual_facts"][0]
        assert {
            "chapter_id",
            "view_kinds",
            "face_coverage",
            "mean_d_hist",
            "motion_level",
            "quality_risks",
        } <= set(fact)
        metrics = services._take_vision_metrics()
        assert metrics["route"] == "talking_head"
        assert metrics["view_kinds"][0] == "bbox_overlay"
        assert len(metrics["view_kinds"]) == 2
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_most_anomalous_moment_uses_measurement_score() -> None:
    results = [
        {
            "available": True,
            "evidence": {
                "tile_map": {
                    "tiles": [
                        {"t": {"value": 90_000, "rate": 90_000}},
                        {"t": {"value": 180_000, "rate": 90_000}},
                        {"t": {"value": 270_000, "rate": 90_000}},
                    ]
                },
                "measurements": [
                    {"tile_index": 1, "score": 0.2},
                    {"tile_index": 2, "score": 0.9},
                    {"tile_index": 3, "score": 0.4},
                ],
            },
        }
    ]
    moment = VisualMixin._most_anomalous_moment("roi_crop", results)
    assert moment is not None and moment.to_float() == 2.0
    assert VisualMixin._most_anomalous_moment("contact_sheet", results) is None
    assert VisualMixin._most_anomalous_moment("roi_crop", []) is None


class _TwoPassVision:
    available = True
    model = "fake-two-pass"

    def __init__(self) -> None:
        self.batches: list[list[dict]] = []

    def analyze(self, messages, schema):
        assert schema is ImageVisualNotes
        self.batches.append(messages)
        count = sum(
            1 for part in messages[1]["content"] if part.get("type") == "image_url"
        )
        first_pass = len(self.batches) == 1
        cautions = ["模糊", "遮挡"] if first_pass else []
        return ImageVisualNotes(
            images=[
                ImageVisualNote(
                    image_index=index,
                    summary=f"画面 {index}",
                    cautions=list(cautions),
                )
                for index in range(count)
            ]
        )

    def close(self) -> None:
        pass


def _batch_text(messages: list[dict]) -> str:
    return "\n".join(
        part.get("text", "")
        for part in messages[1]["content"]
        if part.get("type") == "text"
    )


def _batch_labels(messages: list[dict]) -> set[str]:
    text = _batch_text(messages)
    return set(re.findall(r"\[(T\d{2,3})\|", text))


def test_second_pass_switches_kind_and_scopes_chapters(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    settings = settings.model_copy(
        update={
            "vision_use_evidence": True,
            "view_evidence_enabled": True,
            "vision_second_pass_cautions": 1,
        }
    )
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    fake = _TwoPassVision()
    services.vision = fake
    try:
        project_id = services.create_project("second-pass")["id"]
        services.ingest(project_id, str(synthetic_video))
        services.extract_frames(project_id)
        services.build_chapters(project_id)
        chapters = services.get_chapters(project_id)
        assert chapters is not None

        result = services.build_visual_notes(project_id, "口播讲解", chapters)
        assert result is not None
        assert len(fake.batches) == 2, [
            _batch_text(batch) for batch in fake.batches
        ]
        first, deep = fake.batches
        first_text = _batch_text(first)
        deep_text = _batch_text(deep)
        assert "请复核" in deep_text
        assert "视觉证据视图[bbox_overlay]" in first_text
        assert "视觉证据视图[contact_sheet]" in deep_text
        assert "视觉证据视图[roi_crop]" in deep_text
        assert _batch_labels(first).isdisjoint(_batch_labels(deep))
        assert max(int(label[1:]) for label in _batch_labels(first)) < min(
            int(label[1:]) for label in _batch_labels(deep)
        )
        metrics = services._take_vision_metrics()
        assert metrics["second_pass_images"] > 0
        assert metrics["second_pass_chapters"] > 0
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_roi_target_time_selects_requested_moment(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    settings = settings.model_copy(
        update={"vision_use_evidence": True, "view_evidence_enabled": True}
    )
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project_id = services.create_project("roi-target")["id"]
        services.ingest(project_id, str(synthetic_video))
        services.extract_frames(project_id)
        target = RationalTime.from_seconds("1.0", rate=90_000)

        def resolver(kind, _results):
            return target if kind == "roi_crop" else None

        results = services.build_visual_evidence_set(
            project_id,
            ["contact_sheet", "roi_crop"],
            dedupe=False,
            roi_target_resolver=resolver,
        )
        roi = next(item for item in results if item["kind"] == "roi_crop")
        moment = seconds_from_mapping(
            roi["evidence"]["tile_map"]["tiles"][0]["t"]
        )
        assert moment is not None
        assert abs(float(moment) - 1.0) <= 0.6
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_evidence_set_shares_extraction_and_caches(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    settings = settings.model_copy(
        update={"vision_use_evidence": True, "view_evidence_enabled": True}
    )
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project_id = services.create_project("evidence-set")["id"]
        services.ingest(project_id, str(synthetic_video))
        services.extract_frames(project_id)

        calls: list[dict] = []
        original = services._collect_tiles

        def counting(*args, **kwargs):
            calls.append(dict(kwargs))
            return original(*args, **kwargs)

        services._collect_tiles = counting
        kinds = ["contact_sheet", "bbox_overlay", "roi_crop", "codec_residual"]
        results = services.build_visual_evidence_set(
            project_id,
            kinds,
            label_start=1,
        )
        assert all(result["available"] for result in results), results
        # 原始帧共享一次；codec_residual 单独一次；ROI 高分辨率帧按不变量单独抽。
        shared = [
            item
            for item in calls
            if not item.get("codecview") and item.get("width") is None
        ]
        codec = [item for item in calls if item.get("codecview")]
        roi = [item for item in calls if item.get("width")]
        assert len(shared) == 1
        assert len(codec) == 1
        assert len(roi) == 1

        labels = [
            tile["label"]
            for result in results
            for tile in result["evidence"]["tile_map"]["tiles"]
        ]
        assert labels and len(labels) == len(set(labels))

        calls.clear()
        again = services.build_visual_evidence_set(
            project_id,
            kinds,
            label_start=1,
        )
        assert all(result["available"] for result in again), again
        assert calls == []
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
