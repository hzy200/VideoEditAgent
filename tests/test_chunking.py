from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from smartcut.cancel import CancellationToken
from smartcut.config import Settings
from smartcut.contracts.time import RationalTime
from smartcut.contracts.transcript import Transcript, TranscriptSegment, Word
from smartcut.errors import RunCancelledError
from smartcut.perception.chunking import merge_transcripts, split_wav
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def test_build_transcriber_uses_process_worker(settings: Settings, monkeypatch) -> None:
    from smartcut.perception import transcribe

    monkeypatch.setattr(
        transcribe.FasterWhisperTranscriber,
        "available",
        staticmethod(lambda: True),
    )
    transcriber = transcribe.build_transcriber(settings)
    try:
        assert isinstance(transcriber, transcribe.ProcessWhisperTranscriber)
        clone = transcriber.clone()
        assert isinstance(clone, transcribe.ProcessWhisperTranscriber)
        assert clone is not transcriber
        clone.close()
    finally:
        transcriber.close()


def test_process_whisper_cancel_kills_worker() -> None:
    from smartcut.perception import transcribe

    class FakeProcess:
        def __init__(self) -> None:
            self.killed = False

        def is_alive(self) -> bool:
            return True

        def kill(self) -> None:
            self.killed = True

        def join(self, timeout: float | None = None) -> None:
            return None

    class FakeConn:
        def poll(self, timeout: float | None = None) -> bool:
            return False

        def close(self) -> None:
            return None

    token = CancellationToken()
    token.cancel()
    worker = transcribe.ProcessWhisperTranscriber()
    fake = FakeProcess()
    worker._process = fake
    worker._conn = FakeConn()
    with pytest.raises(RunCancelledError):
        worker._await_response(token)
    assert fake.killed is True


def test_process_whisper_retries_once_after_worker_exit(monkeypatch) -> None:
    from smartcut.perception import transcribe

    class FakeConn:
        def send(self, value) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeProcess:
        def __init__(self) -> None:
            self.alive = True

        def is_alive(self) -> bool:
            return self.alive

        def kill(self) -> None:
            self.alive = False

        def join(self, timeout: float | None = None) -> None:
            return None

    worker = transcribe.ProcessWhisperTranscriber()
    calls: list[int] = []

    def ensure_worker() -> None:
        worker._conn = FakeConn()
        worker._process = FakeProcess()

    def await_response(_token):
        calls.append(1)
        if len(calls) == 1:
            raise transcribe._WhisperWorkerExited("worker exited")
        return {
            "ok": True,
            "transcript": Transcript(
                language="en", words=[], segments=[], degraded=False
            ).model_dump(mode="json"),
        }

    monkeypatch.setattr(worker, "_ensure_worker", ensure_worker)
    monkeypatch.setattr(worker, "_await_response", await_response)
    result = worker.transcribe(Path("unused.wav"))
    assert calls == [1, 1]
    assert result.degraded is False


def test_process_whisper_retries_after_timeout(monkeypatch) -> None:
    from smartcut.perception import transcribe

    class FakeConn:
        def send(self, value) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeProcess:
        def __init__(self) -> None:
            self.alive = True

        def is_alive(self) -> bool:
            return self.alive

        def kill(self) -> None:
            self.alive = False

        def join(self, timeout: float | None = None) -> None:
            return None

    worker = transcribe.ProcessWhisperTranscriber(retries=1)
    calls: list[int] = []

    def ensure_worker() -> None:
        worker._conn = FakeConn()
        worker._process = FakeProcess()

    def await_response(_token):
        calls.append(1)
        if len(calls) == 1:
            raise transcribe._WhisperWorkerTimeout("timeout")
        return {
            "ok": True,
            "transcript": Transcript(
                language="en", words=[], segments=[], degraded=False
            ).model_dump(mode="json"),
        }

    monkeypatch.setattr(worker, "_ensure_worker", ensure_worker)
    monkeypatch.setattr(worker, "_await_response", await_response)
    result = worker.transcribe(Path("unused.wav"))
    assert calls == [1, 1]
    assert result.degraded is False


def test_services_clone_transcriber_per_job(
    settings: Settings,
    tmp_path: Path,
) -> None:
    class CloningTranscriber(FakeChunkTranscriber):
        def __init__(self) -> None:
            self.clones = 0

        def clone(self):
            self.clones += 1
            return CloningTranscriber()

    db = Database(tmp_path / "clone.sqlite")
    db.init()
    jobs = JobService(db, 1)
    root = CloningTranscriber()
    services = Services(settings, db, jobs, transcriber=root)
    try:
        first = services._new_transcriber()
        second = services._new_transcriber()
        assert first is not root and second is not root
        assert first is not second
        assert root.clones == 2
    finally:
        services.close()
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        db.dispose()


class FakeChunkTranscriber:
    name = "fake-chunk"
    degraded = False
    cache_identity = "fake-chunk-v1"

    def transcribe(self, audio_path: Path) -> Transcript:
        word = Word(
            text="chunk",
            start=RationalTime.from_seconds("0.1", rate=90_000),
            end=RationalTime.from_seconds("0.2", rate=90_000),
        )
        segment = TranscriptSegment(
            id=0,
            text="chunk",
            start=word.start,
            end=word.end,
            words=[word],
        )
        return Transcript(language="en", words=[word], segments=[segment], degraded=False)


class ParallelTranscriber:
    name = "parallel"
    degraded = False
    cache_identity = "parallel-v1"

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls = 0

    def clone(self) -> "ParallelTranscriber":
        return self

    def transcribe(self, audio_path: Path) -> Transcript:
        with self.lock:
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.15)
            return FakeChunkTranscriber().transcribe(audio_path)
        finally:
            with self.lock:
                self.active -= 1


class ExplodingTranscriber:
    name = "exploding"
    degraded = False
    cache_identity = "exploding-v1"

    def transcribe(self, audio_path: Path) -> Transcript:
        raise AssertionError("静音块不应调用转写模型")


def _make_video(path: Path, ffmpeg_bin: str, *, silent: bool, seconds: int = 6) -> None:
    audio = (
        ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono"]
        if silent
        else ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    )
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=320x240:rate=30:duration={seconds}",
        *audio,
        "-t",
        str(seconds),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    ]
    subprocess.run(command, check=True, capture_output=True)


def test_split_wav_and_merge_offsets(tmp_path: Path, ffmpeg_bin: str) -> None:
    audio = tmp_path / "six.wav"
    subprocess.run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(audio),
        ],
        check=True,
        capture_output=True,
    )

    chunks = split_wav(
        audio,
        tmp_path / "chunks",
        chunk_seconds=2.0,
        overlap_seconds=0.5,
    )
    assert len(chunks) == 3
    assert all(chunk.path.exists() for chunk in chunks)
    assert chunks[0].start == 0.0

    items = []
    for chunk in chunks:
        word = Word(
            text=f"c{chunk.index}",
            start=RationalTime.from_seconds("0.1", rate=90_000),
            end=RationalTime.from_seconds("0.2", rate=90_000),
        )
        segment = TranscriptSegment(
            id=chunk.index,
            text=word.text,
            start=word.start,
            end=word.end,
            words=[word],
        )
        items.append(
            (
                chunk,
                Transcript(
                    language="en",
                    words=[word],
                    segments=[segment],
                    degraded=False,
                ),
            )
        )

    merged = merge_transcripts(items)
    starts = [word.start.to_float() for word in merged.words]
    assert starts == sorted(starts)
    assert len(merged.words) == 3


def test_services_chunk_transcription_uses_cache(
    settings: Settings,
    tmp_path: Path,
    ffmpeg_bin: str,
) -> None:
    settings = settings.model_copy(
        update={
            "transcribe_chunk_seconds": 2.0,
            "transcribe_chunk_overlap_seconds": 0.0,
        }
    )
    video = tmp_path / "six.mp4"
    subprocess.run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=30:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ],
        check=True,
        capture_output=True,
    )

    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(
        settings,
        db,
        jobs,
        transcriber=FakeChunkTranscriber(),
    )
    try:
        project = services.create_project("chunk")
        project_id = project["id"]
        services.ingest(project_id, str(video))

        first = services.transcribe(project_id)
        assert first["chunks"] >= 2
        assert first["cache_hits"] == 0

        second = services.transcribe(project_id)
        assert second["chunks"] == first["chunks"]
        assert second["cache_hits"] == first["chunks"]
        assert second["words"] >= 1
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_transcription_parallelizes_chunks(
    settings: Settings,
    tmp_path: Path,
    ffmpeg_bin: str,
) -> None:
    settings = settings.model_copy(
        update={
            "transcribe_chunk_seconds": 2.0,
            "transcribe_chunk_overlap_seconds": 0.0,
            "transcribe_workers": 2,
        }
    )
    video = tmp_path / "six.mp4"
    _make_video(video, ffmpeg_bin, silent=False, seconds=6)

    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    transcriber = ParallelTranscriber()
    services = Services(settings, db, jobs, transcriber=transcriber)
    try:
        project = services.create_project("parallel")
        project_id = project["id"]
        services.ingest(project_id, str(video))
        result = services.transcribe(project_id)
        assert result["chunks"] >= 3
        assert result["transcribe_workers"] == 2
        assert result["cache_misses"] == result["chunks"]
        assert transcriber.max_active == 2
        assert result["words"] >= 1
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_silent_chunks_skip_transcription(
    settings: Settings,
    tmp_path: Path,
    ffmpeg_bin: str,
) -> None:
    settings = settings.model_copy(
        update={
            "transcribe_chunk_seconds": 300.0,
            "transcribe_chunk_overlap_seconds": 0.0,
        }
    )
    video = tmp_path / "silent.mp4"
    _make_video(video, ffmpeg_bin, silent=True, seconds=2)

    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=ExplodingTranscriber())
    try:
        project = services.create_project("silent")
        project_id = project["id"]
        services.ingest(project_id, str(video))
        first = services.transcribe(project_id)
        assert first["silent_chunks"] == 1
        assert first["words"] == 0
        assert first["cache_misses"] == 0

        second = services.transcribe(project_id)
        assert second["cache_hits"] == second["chunks"]
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
