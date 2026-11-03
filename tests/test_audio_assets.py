from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from smartcut.errors import ResourceNotFound

from smartcut.config import Settings
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.main import create_app


def _wav(path: Path, ffmpeg_bin: str) -> Path:
    subprocess.run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:duration=1",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_import_audio_asset_roundtrip_and_cascade(
    settings: Settings, synthetic_video: Path, ffmpeg_bin: str, tmp_path: Path
) -> None:
    app = create_app(settings, transcriber=NullTranscriber())
    bgm = _wav(tmp_path / "bgm.wav", ffmpeg_bin)
    with TestClient(app) as client:
        services = app.state.services
        project_id = client.post("/api/projects", json={"name": "audio"}).json()["id"]
        ingested = client.post(
            f"/api/projects/{project_id}/ingest",
            json={"src": str(synthetic_video)},
        ).json()
        facts = ingested["facts"]

        imported = services.import_audio_asset(project_id, str(bgm), "BGM")
        assert imported["name"] == "BGM"
        assert imported["src"].startswith("audio/")
        assert imported["facts"]["audio"] is not None

        assets = services.list_audio_assets(project_id)
        assert [item["id"] for item in assets] == [imported["id"]]
        bundle = services.get_project(project_id)
        assert bundle["audio_assets"][0]["name"] == "BGM"

        response = client.get(f"/api/projects/{project_id}/audio-assets")
        assert response.status_code == 200
        assert response.json()["audio_assets"][0]["name"] == "BGM"

        via_api = client.post(
            f"/api/projects/{project_id}/audio-assets",
            json={"src": str(bgm), "name": "BGM API"},
        )
        assert via_api.status_code == 200, via_api.text
        assert len(services.list_audio_assets(project_id)) == 2

        ir = {
            "schema": "editir/v1",
            "project_id": project_id,
            "revision": 0,
            "media_facts": facts,
            "sequence": [
                {
                    "op": "trim",
                    "src": facts["src"],
                    "range": {
                        "start": {"value": 0, "rate": 90_000},
                        "end": {"value": 90_000, "rate": 90_000},
                    },
                }
            ],
            "audio": {
                "tracks": [
                    {
                        "id": "main",
                        "role": "main",
                        "asset_id": "main",
                        "source_end": {"value": 90_000, "rate": 90_000},
                    },
                    {
                        "id": "bgm",
                        "role": "music",
                        "asset_id": imported["id"],
                        "source_end": {"value": 45_000, "rate": 90_000},
                        "gain_db": -8,
                    },
                ]
            },
            "export": {"preset": "mp4_h264", "cut_mode": "reencode"},
        }
        created = client.post(
            f"/api/projects/{project_id}/revisions", json={"ir": ir}
        )
        assert created.status_code == 200, created.text
        dry = client.post(
            f"/api/projects/{project_id}/render",
            json={"revision": created.json()["revision"], "dry_run": True},
        )
        assert dry.status_code == 200, dry.text
        calls = dry.json()["tool_calls"]
        assert [call["tool"] for call in calls] == [
            "probe",
            "cut",
            "mix_audio",
            "verify",
        ]
        mix = next(call for call in calls if call["tool"] == "mix_audio")
        assert [track["src"] for track in mix["args"]["tracks"]] == [
            facts["src"],
            imported["src"],
        ]

        revision_no = created.json()["revision"]
        stored_ir = services.get_revision(project_id, revision_no)
        preview = services.build_preview_mix(project_id, stored_ir)
        assert Path(preview["path"]).exists()
        assert preview["cached"] is False
        again = services.build_preview_mix(project_id, stored_ir)
        assert again["cached"] is True
        assert again["path"] == preview["path"]

        waveforms = services.build_audio_waveforms(project_id, buckets=128)
        assert waveforms["main"]
        assert waveforms["assets"][imported["id"]]

        services.delete_project(project_id)
        with pytest.raises(ResourceNotFound):
            services.list_audio_assets(project_id)
        # 项目删除后再次查询应 404，音频资产随工作区/记录一起消失。
        assert client.get(f"/api/projects/{project_id}/audio-assets").status_code == 404
