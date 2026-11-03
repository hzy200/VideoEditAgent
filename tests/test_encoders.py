from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from smartcut.compiler.encoding import video_encoder_args
from smartcut.compiler.lower import lower
from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.encoder import EncoderProfile
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.contracts.tools import ToolCall, ToolResult
from smartcut.errors import ToolExecutionError
from smartcut.perception.transcribe import NullTranscriber
from smartcut.policy import load_policy
from smartcut.runner.encoders import choose_profile, parse_encoder_list
from smartcut.runner.tools.cut import CutArgs, build_argv
from smartcut.runner.tools.base import ToolContext
from smartcut.runner.paths import Workspace
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services
from smartcut.server.services.rendering import RenderMixin


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def test_final_actual_encoder_uses_final_output_step() -> None:
    plan = SimpleNamespace(output_path="renders/r1/output.mp4")
    calls = [
        ToolCall(
            seq=0,
            tool="cut",
            args={
                "dst": "renders/r1/seg_000.mp4",
                "encoder": {"codec": "h264_nvenc"},
            },
        ),
        ToolCall(
            seq=1,
            tool="compose",
            args={"dst": "renders/r1/output.mp4"},
        ),
    ]
    results = {
        0: ToolResult(
            tool="cut",
            argv=[],
            exit_code=0,
            duration_ms=1,
            artifacts=["renders/r1/seg_000.mp4"],
            encoder="h264_nvenc",
        ),
        1: ToolResult(
            tool="compose",
            argv=[],
            exit_code=0,
            duration_ms=1,
            artifacts=["renders/r1/output.mp4"],
            encoder="libx264",
        ),
    }
    assert (
        RenderMixin._final_actual_encoder(plan, calls, results) == "libx264"
    )

    results[1] = results[1].model_copy(update={"encoder": None})
    assert RenderMixin._final_actual_encoder(plan, calls, results) == "copy"


def make_ir() -> EditIR:
    facts = MediaFacts(
        src="media/a.mp4",
        duration=_rt("10"),
        fps="30/1",
        video=StreamFacts(codec_type="video", width=1280, height=720, avg_frame_rate="30/1"),
        audio=StreamFacts(codec_type="audio", channels=2),
    )
    return EditIR(
        project_id="p_test",
        revision=1,
        media_facts=facts,
        sequence=[
            TrimOp(
                src=facts.src,
                range=TimeRange(start=_rt("1"), end=_rt("3")),
                tag="keep",
                confidence=0.9,
            )
        ],
    )


def test_encoder_argv_profiles() -> None:
    nvenc = video_encoder_args(EncoderProfile(codec="h264_nvenc", preset="p4", quality=20))
    assert nvenc[:4] == ["-c:v", "h264_nvenc", "-preset", "p4"]
    assert "-cq" in nvenc and "20" in nvenc and "-b:v" in nvenc
    qsv = video_encoder_args(EncoderProfile(codec="h264_qsv", quality=21))
    assert "-global_quality" in qsv and "21" in qsv
    amf = video_encoder_args(EncoderProfile(codec="h264_amf", preset="balanced", quality=22))
    assert "-qp_i" in amf and "-qp_p" in amf and "22" in amf
    x264 = video_encoder_args(EncoderProfile(codec="libx264", preset="veryfast", quality=18))
    assert x264 == ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]


def test_parse_and_choose_encoder_prefers_hardware() -> None:
    text = """
 V....D libx264              H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder
 V....D h264_qsv             Intel Quick Sync H.264 encoder
"""
    available = parse_encoder_list(text)
    assert {"libx264", "h264_nvenc", "h264_qsv"} <= available
    policy = load_policy()
    profile = choose_profile(
        policy,
        available,
        ["h264_nvenc", "h264_qsv", "libx264"],
    )
    assert profile.codec == "h264_nvenc"
    fallback = choose_profile(policy, {"libx264"}, ["h264_nvenc", "libx264"])
    assert fallback.codec == "libx264"
    off = choose_profile(policy, available, ["h264_nvenc"], enabled=False)
    assert off.codec == "libx264"


class FakeProbeProcess:
    def __init__(self, failing: set[str]) -> None:
        self.failing = failing
        self.tested: list[str] = []

    def run(self, argv, *, timeout, tool, cancel_token=None):
        if tool == "encoder_probe":
            return SimpleNamespace(
                stdout=(
                    " V....D libx264 H.264\n"
                    " V....D h264_nvenc NVIDIA\n"
                    " V....D h264_qsv Intel\n"
                    " V....D h264_amf AMD\n"
                ),
                stderr="",
            )
        codec = argv[argv.index("-c:v") + 1]
        self.tested.append(codec)
        if codec in self.failing:
            raise ToolExecutionError(f"{codec} unavailable")
        return SimpleNamespace(stdout="", stderr="")


def test_encoder_probe_test_encodes_hardware(tmp_path: Path) -> None:
    from smartcut.runner.encoders import EncoderSelector

    settings = Settings(
        workspace_root=tmp_path / "workspace",
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        deepseek_api_key=None,
        hardware_encoding="auto",
        encoder_priority="h264_nvenc,h264_qsv,h264_amf,libx264",
    )
    process = FakeProbeProcess(failing={"h264_nvenc"})
    selector = EncoderSelector(settings, load_policy())
    available = selector.detect(process)
    assert "h264_nvenc" not in available
    assert {"h264_qsv", "h264_amf", "libx264"} <= available
    assert "h264_nvenc" in process.tested
    assert selector.resolve(process).codec == "h264_qsv"


def test_lower_injects_encoder_only_when_requested() -> None:
    ir = make_ir()
    plain = lower(ir)
    assert all("encoder" not in call.args for call in plain.tool_calls)

    profile = EncoderProfile(codec="h264_nvenc", preset="p4", quality=20)
    encoded = lower(ir, encoder=profile)
    cut = next(call for call in encoded.tool_calls if call.tool == "cut")
    assert cut.args["encoder"]["codec"] == "h264_nvenc"
    assert cut.args["encoder"]["quality"] == 20


def test_cut_argv_uses_encoder_profile(
    workspace: Workspace,
    settings: Settings,
) -> None:
    (workspace.media_dir / "a.mp4").write_bytes(b"fake")
    args = CutArgs(
        src="media/a.mp4",
        dst="renders/r1/out.mp4",
        start=_rt("0"),
        end=_rt("1"),
        encoder=EncoderProfile(codec="h264_qsv", preset="veryfast", quality=20),
    )
    argv = build_argv(args, ToolContext(settings=settings, workspace=workspace))
    joined = " ".join(argv)
    assert "-c:v h264_qsv" in joined
    assert "-global_quality 20" in joined


def test_proxy_is_generated_once_and_readable_first(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project = services.create_project("proxy-first")
        project_id = project["id"]
        services.ingest(project_id, str(synthetic_video))
        assert services.latest_proxy(project_id) is None

        first = services.create_proxy(project_id, height=360)
        assert first["cached"] is False
        assert first["encoder"] == "libx264"
        latest = services.latest_proxy(project_id)
        assert latest is not None
        assert latest["proxy"] == first["proxy"]

        second = services.create_proxy(project_id, height=360)
        assert second["cached"] is True
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_proxy_uses_settings_height_when_unspecified(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    settings.proxy_height = 240
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project_id = services.create_project("proxy-config")["id"]
        services.ingest(project_id, str(synthetic_video))

        first = services.create_proxy(project_id)
        second = services.create_proxy(project_id, height=settings.proxy_height)
        assert first["cached"] is False
        assert second["cached"] is True
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_render_falls_back_to_software(tmp_path: Path) -> None:
    settings = Settings(
        workspace_root=tmp_path / "workspace",
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        deepseek_api_key=None,
        hardware_encoding="off",
    )
    settings.ensure_dirs()
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())

    class FakeExecutor:
        def __init__(self) -> None:
            self.calls: list[ToolCall] = []

        def execute(self, call: ToolCall) -> ToolResult:
            self.calls.append(call)
            if "encoder" in call.args:
                raise ToolExecutionError("h264_nvenc initialization failed")
            return ToolResult(
                tool=call.tool,
                argv=[],
                exit_code=0,
                duration_ms=1,
                artifacts=[call.args["dst"]],
            )

    try:
        executor = FakeExecutor()
        call = ToolCall(
            seq=0,
            tool="cut",
            args={
                "src": "media/a.mp4",
                "dst": "renders/r1/out.mp4",
                "start": {"value": 0, "rate": 90_000},
                "end": {"value": 90_000, "rate": 90_000},
                "encoder": {"codec": "h264_nvenc", "preset": "p4", "quality": 20},
            },
        )
        executed, result, warning = services._execute_with_fallback(call, executor)
        assert warning and "h264_nvenc" in warning
        assert "encoder" not in executed.args
        assert "h264_nvenc" in services._failed_encoders
        assert result.tool == "cut"
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
