from __future__ import annotations

from pathlib import Path

import pytest

from smartcut.config import Settings
from smartcut.errors import ToolExecutionError
from smartcut.perception import transcribe as transcribe_mod
from smartcut.perception.transcribe import FasterWhisperTranscriber, NullTranscriber
from smartcut.runner.decoders import HardwareDecodeSelector
from smartcut.runner.execute import ToolCallExecutor
from smartcut.runner.paths import Workspace
from smartcut.runner.tools.base import ToolContext
from smartcut.runner.tools.frames import ExtractFramesArgs, build_argv as frames_argv
from smartcut.runner.tools.proxy import ProxyArgs, build_argv as proxy_argv
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


class _FakeProcess:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    def run(self, argv, *, timeout, tool, cancel_token=None):
        self.calls.append(argv)
        if self.fail:
            raise ToolExecutionError("gpu unavailable")
        return type("R", (), {"stdout": "", "stderr": "", "exit_code": 0})()


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        workspace_root=tmp_path / "workspace",
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        hardware_decoding="auto",
        decode_hwaccel="cuda",
        deepseek_api_key=None,
    )
    values.update(overrides)
    return Settings(**values)


def test_hwaccel_argv_is_injected(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = Workspace(tmp_path / "workspace")
    workspace.root.mkdir(parents=True, exist_ok=True)
    (workspace.media_dir).mkdir(parents=True, exist_ok=True)
    (workspace.media_dir / "a.mp4").write_bytes(b"x")
    ctx = ToolContext(settings=settings, workspace=workspace)

    frames = frames_argv(
        ExtractFramesArgs(
            src="media/a.mp4",
            dst_dir="frames/x",
            hwaccel="cuda",
        ),
        ctx,
    )
    assert frames[frames.index("-hwaccel") + 1] == "cuda"

    proxy = proxy_argv(
        ProxyArgs(
            src="media/a.mp4",
            dst="proxy/a.mp4",
            hwaccel="cuda",
        ),
        ctx,
    )
    assert proxy[proxy.index("-hwaccel") + 1] == "cuda"


def test_decode_selector_caches_failure_and_resets(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    selector = HardwareDecodeSelector(settings)
    failing = _FakeProcess(fail=True)
    assert selector.available(failing, "media/a.mp4") is False
    # 失败结果按后端缓存，不再启动新进程。
    assert selector.available(failing, "media/a.mp4") is False
    assert len(failing.calls) == 1
    selector.reset()
    assert selector.available(_FakeProcess(fail=False), "media/a.mp4") is True


def test_extract_frames_falls_back_to_cpu(
    settings: Settings,
    synthetic_video: Path,
    monkeypatch,
) -> None:
    settings = settings.model_copy(
        update={"hardware_decoding": "auto", "decode_hwaccel": "cuda"}
    )
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    monkeypatch.setattr(
        services.decode_selector,
        "available",
        lambda *_args, **_kwargs: True,
    )
    original = ToolCallExecutor.execute
    attempts: list[bool] = []

    def flaky(self, call):
        if call.tool == "extract_frames":
            attempts.append("hwaccel" in call.args)
            if "hwaccel" in call.args:
                raise ToolExecutionError("cuda decode failed")
        return original(self, call)

    monkeypatch.setattr(ToolCallExecutor, "execute", flaky)
    try:
        project = services.create_project("gpu-fallback")
        project_id = project["id"]
        services.ingest(project_id, str(synthetic_video))
        result = services.extract_frames(project_id)
        assert result["available"] is True
        assert result["decoder"] == "cpu"
        assert result["decode_fallback"] is True
        assert result["warnings"]
        assert attempts[:2] == [True, False]
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_transcriber_auto_device_is_lazy(monkeypatch) -> None:
    monkeypatch.setattr(transcribe_mod, "_cuda_available", lambda: True)
    cuda = FasterWhisperTranscriber(device="auto")
    assert cuda.resolved_device == "cuda"
    assert "cuda" in cuda.cache_identity

    monkeypatch.setattr(transcribe_mod, "_cuda_available", lambda: False)
    cpu = FasterWhisperTranscriber(device="auto")
    assert cpu.resolved_device == "cpu"


class _FakeCudaTranscriber:
    name = "fake-cuda"
    degraded = False
    resolved_device = "cuda"
    thread_safe = True

    def transcribe(self, audio_path):  # pragma: no cover - 仅用于 worker 策略
        return NullTranscriber().transcribe(audio_path)

    def clone(self):
        return self


class _FakeCpuTranscriber(_FakeCudaTranscriber):
    resolved_device = "cpu"


@pytest.mark.parametrize(
    ("transcriber_cls", "expected"),
    [(_FakeCudaTranscriber, 1), (_FakeCpuTranscriber, 2)],
)
def test_gpu_transcription_stays_single_worker(
    settings: Settings,
    tmp_path: Path,
    transcriber_cls,
    expected: int,
) -> None:
    settings = settings.model_copy(
        update={"transcribe_workers": 0, "whisper_device": "auto"}
    )
    db = Database(tmp_path / "gpu.sqlite")
    db.init()
    jobs = JobService(db, 1)
    services = Services(
        settings,
        db,
        jobs,
        transcriber=transcriber_cls(),
    )
    try:
        assert services._effective_transcribe_workers(4) == expected
        assert services._transcribe_device() in {"cpu", "cuda"}
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
