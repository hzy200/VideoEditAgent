"""转写接口。faster-whisper 是 optional extra，缺失时降级为空稿而不是 500。"""

from __future__ import annotations

import importlib.util
import multiprocessing
import threading
import time
import wave
from pathlib import Path
from typing import Any, Protocol

from smartcut.config import Settings
from smartcut.contracts.time import RationalTime
from smartcut.contracts.transcript import Transcript, TranscriptSegment, Word
from smartcut.errors import RunCancelledError, ToolExecutionError
from smartcut.logging import get_logger

_LOG = get_logger("smartcut.transcribe")


class Transcriber(Protocol):
    name: str
    degraded: bool

    def transcribe(self, audio_path: Path) -> Transcript: ...


class NullTranscriber:
    """未安装 faster-whisper 时的确定性降级实现。"""

    name = "null"
    degraded = True
    cache_identity = "null"

    def transcribe(self, audio_path: Path) -> Transcript:
        return Transcript(
            language=None,
            words=[],
            segments=[],
            degraded=True,
            note="未安装 faster-whisper，转写降级为空稿；可 pip install -e '.[transcribe]'",
        )


def _cuda_available() -> bool:
    """只查询 CTranslate2 CUDA 设备数；不 import torch。"""
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count()) > 0
    except Exception:
        return False


def _read_wav_16k_mono(audio_path: Path):
    """读取 extract_audio 生成的 16k 单声道 PCM，避开 PyAV 版本的解码兼容问题。"""
    try:
        import numpy as np
    except ImportError:
        return None
    try:
        with wave.open(str(audio_path), "rb") as handle:
            if (
                handle.getframerate() != 16_000
                or handle.getnchannels() != 1
                or handle.getsampwidth() != 2
            ):
                return None
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, OSError, ValueError):
        return None
    if not frames:
        return None
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


class FasterWhisperTranscriber:
    name = "faster-whisper"
    degraded = False

    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        gpu_compute_type: str = "float16",
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._gpu_compute_type = gpu_compute_type
        self._resolved_device: str | None = None
        self._model = None
        self._model_lock = threading.Lock()

    @property
    def resolved_device(self) -> str:
        if str(self._device).lower() != "auto":
            return str(self._device).lower()
        if self._resolved_device is None:
            self._resolved_device = "cuda" if _cuda_available() else "cpu"
        return self._resolved_device

    @property
    def cache_identity(self) -> str:
        return (
            f"faster-whisper:{self._model_size}:"
            f"{self.resolved_device}:{self._compute_type}"
        )

    def clone(self) -> "FasterWhisperTranscriber":
        """为并行分块创建独立模型实例，避免共享 CTranslate2 模型。"""
        return FasterWhisperTranscriber(
            model_size=self._model_size,
            device=self._device,
            compute_type=self._compute_type,
            gpu_compute_type=self._gpu_compute_type,
        )

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("faster_whisper") is not None

    def _load_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is None:
                from faster_whisper import WhisperModel  # 函数内懒加载

                device = self.resolved_device
                compute_type = self._compute_type
                if str(compute_type).lower() == "auto":
                    compute_type = (
                        self._gpu_compute_type if device == "cuda" else "int8"
                    )
                self._model = WhisperModel(
                    self._model_size,
                    device=device,
                    compute_type=compute_type,
                )
        return self._model

    def transcribe(self, audio_path: Path) -> Transcript:
        if not self.available():
            return NullTranscriber().transcribe(audio_path)
        return _run_model(self._load_model(), audio_path)


def _run_model(model, audio_path: Path) -> Transcript:
    """在 worker 进程内执行一次解码；model 不离开该进程。"""
    audio_input = _read_wav_16k_mono(audio_path)
    if audio_input is None:
        audio_input = str(audio_path)
    try:
        segments_iter, info = model.transcribe(
            audio_input,
            word_timestamps=True,
            vad_filter=True,
        )
    except TypeError as exc:
        if "metadata_errors" in str(exc):
            raise RuntimeError(
                "当前 PyAV 与 faster-whisper 版本不兼容；"
                "请通过 extract_audio 生成 16k 单声道 WAV 后再转写"
            ) from exc
        raise

    words: list[Word] = []
    segments: list[TranscriptSegment] = []
    for segment in segments_iter:
        segment_words: list[Word] = []
        for word in segment.words or []:
            item = Word(
                text=word.word,
                start=RationalTime.from_seconds(word.start, rate=90_000),
                end=RationalTime.from_seconds(word.end, rate=90_000),
                probability=(
                    float(word.probability)
                    if word.probability is not None
                    else None
                ),
            )
            segment_words.append(item)
        words.extend(segment_words)
        segments.append(
            TranscriptSegment(
                id=getattr(segment, "id", None),
                text=segment.text,
                start=RationalTime.from_seconds(segment.start, rate=90_000),
                end=RationalTime.from_seconds(segment.end, rate=90_000),
                words=segment_words,
            )
        )

    return Transcript(
        language=getattr(info, "language", None),
        words=words,
        segments=segments,
        degraded=False,
    )


class _WhisperWorkerExited(ToolExecutionError):
    """worker 进程崩溃/管道断开；允许重启一次后重试。"""


class _WhisperWorkerTimeout(ToolExecutionError):
    """单次请求超时；按配置做受控重试。"""


def _whisper_worker_entry(conn: Any, config: dict[str, Any]) -> None:
    """独立进程入口：模型只在本进程加载与调用。"""
    try:
        from faster_whisper import WhisperModel  # 仅 worker 进程导入

        device = str(config.get("device") or "cpu")
        if device.lower() == "auto":
            device = "cuda" if _cuda_available() else "cpu"
        compute_type = str(config.get("compute_type") or "int8")
        if compute_type.lower() == "auto":
            compute_type = (
                str(config.get("gpu_compute_type") or "float16")
                if device == "cuda"
                else "int8"
            )
        model = WhisperModel(
            str(config["model"]),
            device=device,
            compute_type=compute_type,
        )
        while True:
            try:
                request = conn.recv()
            except EOFError:
                break
            if request is None:
                break
            try:
                transcript = _run_model(model, Path(str(request["audio"])))
                conn.send(
                    {
                        "ok": True,
                        "transcript": transcript.model_dump(mode="json"),
                    }
                )
            except BaseException as exc:
                conn.send(
                    {
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
    except BaseException as exc:
        _LOG.warning(
            "whisper_worker_init_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        try:
            conn.send(
                {
                    "ok": False,
                    "error": f"whisper worker 初始化失败: {type(exc).__name__}: {exc}",
                }
            )
        except Exception as exc:
            _LOG.debug("whisper_worker_error_send_failed", error=str(exc))
    finally:
        try:
            conn.close()
        except Exception as exc:
            _LOG.debug("whisper_worker_conn_close_failed", error=str(exc))


class ProcessWhisperTranscriber:
    """客户端代理：不 import faster-whisper，崩溃/取消只影响 worker 进程。"""

    name = "faster-whisper"
    degraded = False
    supports_cancel_token = True

    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        gpu_compute_type: str = "float16",
        *,
        timeout_seconds: float = 1800.0,
        retries: int = 1,
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._gpu_compute_type = gpu_compute_type
        self.timeout_seconds = float(timeout_seconds)
        self.retries = max(0, int(retries))
        self._resolved_device: str | None = None
        self._process: Any | None = None
        self._conn: Any | None = None
        self._lock = threading.Lock()

    @property
    def resolved_device(self) -> str:
        if str(self._device).lower() != "auto":
            return str(self._device).lower()
        if self._resolved_device is None:
            self._resolved_device = "cuda" if _cuda_available() else "cpu"
        return self._resolved_device

    @property
    def cache_identity(self) -> str:
        return (
            f"faster-whisper:{self._model_size}:"
            f"{self.resolved_device}:{self._compute_type}"
        )

    def clone(self) -> "ProcessWhisperTranscriber":
        return ProcessWhisperTranscriber(
            model_size=self._model_size,
            device=self._device,
            compute_type=self._compute_type,
            gpu_compute_type=self._gpu_compute_type,
            timeout_seconds=self.timeout_seconds,
            retries=self.retries,
        )

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("faster_whisper") is not None

    def _config(self) -> dict[str, Any]:
        return {
            "model": self._model_size,
            "device": self._device,
            "compute_type": self._compute_type,
            "gpu_compute_type": self._gpu_compute_type,
        }

    def _ensure_worker(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        self._kill()
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(
            target=_whisper_worker_entry,
            args=(child, self._config()),
            daemon=True,
        )
        process.start()
        child.close()
        self._conn = parent
        self._process = process

    def _kill(self) -> None:
        process = self._process
        conn = self._conn
        self._process = None
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:
                _LOG.debug("whisper_worker_conn_close_failed", error=str(exc))
        if process is not None:
            if process.is_alive():
                try:
                    process.kill()
                except Exception as exc:
                    _LOG.warning("whisper_worker_kill_failed", error=str(exc))
            process.join(timeout=2)

    def _await_response(self, cancel_token: Any | None) -> dict[str, Any]:
        process = self._process
        conn = self._conn
        if process is None or conn is None:
            raise ToolExecutionError("whisper worker 未启动")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            if cancel_token is not None and cancel_token.cancelled:
                self._kill()
                raise RunCancelledError("whisper 转写已取消")
            try:
                if conn.poll(0.2):
                    message = conn.recv()
                    if isinstance(message, dict):
                        return message
            except (EOFError, OSError) as exc:
                self._kill()
                raise _WhisperWorkerExited(
                    f"whisper worker 通信失败: {exc}"
                ) from exc
            if not process.is_alive():
                self._kill()
                raise _WhisperWorkerExited("whisper worker 意外退出")
            if time.monotonic() > deadline:
                self._kill()
                raise _WhisperWorkerTimeout(
                    f"whisper 转写超时（>{self.timeout_seconds}s）"
                )

    def transcribe(
        self,
        audio_path: Path,
        cancel_token: Any | None = None,
    ) -> Transcript:
        with self._lock:
            message: dict[str, Any] | None = None
            attempts = 0
            while message is None:
                attempts += 1
                self._ensure_worker()
                try:
                    if self._conn is None:
                        raise _WhisperWorkerExited("whisper worker 未启动")
                    self._conn.send({"audio": str(audio_path)})
                except (BrokenPipeError, EOFError, OSError, ValueError) as exc:
                    self._kill()
                    if attempts > self.retries:
                        raise _WhisperWorkerExited(
                            f"whisper worker 发送失败: {exc}"
                        ) from exc
                    continue
                try:
                    message = self._await_response(cancel_token)
                except _WhisperWorkerExited:
                    self._kill()
                    if attempts > self.retries:
                        raise
                    continue
                except _WhisperWorkerTimeout:
                    self._kill()
                    if attempts > self.retries:
                        raise
                    _LOG.warning(
                        "whisper_worker_timeout_retry",
                        attempt=attempts,
                        retries=self.retries,
                        timeout_seconds=self.timeout_seconds,
                    )
                    continue
        if not message.get("ok"):
            raise ToolExecutionError(
                str(message.get("error") or "whisper worker 转写失败")
            )
        payload = message.get("transcript")
        if not isinstance(payload, dict):
            raise ToolExecutionError("whisper worker 返回缺少 transcript")
        return Transcript.model_validate(payload)

    def close(self) -> None:
        with self._lock:
            conn = self._conn
            process = self._process
            if conn is not None:
                try:
                    conn.send(None)
                except Exception as exc:
                    _LOG.debug("whisper_worker_stop_send_failed", error=str(exc))
            self._conn = None
            self._process = None
            if process is not None:
                process.join(timeout=5)
                if process.is_alive():
                    try:
                        process.kill()
                    except Exception as exc:
                        _LOG.warning("whisper_worker_kill_failed", error=str(exc))
                    process.join(timeout=2)
            if conn is not None:
                try:
                    conn.close()
                except Exception as exc:
                    _LOG.debug("whisper_worker_conn_close_failed", error=str(exc))


def build_transcriber(settings: Settings) -> Transcriber:
    if not FasterWhisperTranscriber.available():
        return NullTranscriber()

    model = settings.whisper_model
    if settings.whisper_model_path:
        path = Path(settings.whisper_model_path)
        if not path.exists():
            return NullTranscriber()
        model = str(path)

    return ProcessWhisperTranscriber(
        model_size=model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        gpu_compute_type=settings.whisper_gpu_compute_type,
        timeout_seconds=settings.whisper_worker_timeout_seconds,
        retries=settings.whisper_worker_retries,
    )
