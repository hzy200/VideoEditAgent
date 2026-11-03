from __future__ import annotations

import json
from pathlib import Path

from smartcut import separation_worker


def test_worker_model_dir_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SMARTCUT_DEMUCS_MODEL_DIR", str(tmp_path))
    assert separation_worker._model_dir() == tmp_path


def test_worker_progress_file_is_atomic_json(tmp_path: Path) -> None:
    path = tmp_path / "progress.json"
    separation_worker._write_progress(path, 42, "分离中")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"percent": 42, "message": "分离中"}
    assert not (tmp_path / "progress.tmp").exists()
