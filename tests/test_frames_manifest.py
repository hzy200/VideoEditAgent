from __future__ import annotations

import json
import shutil
from pathlib import Path

from PIL import Image

from smartcut.contracts.tools import ToolCall
from smartcut.perception import contactsheet, frames
from smartcut.runner.execute import ToolCallExecutor
from smartcut.runner.paths import Workspace


def _ingest(workspace: Workspace, video: Path) -> str:
    destination = workspace.media_dir / "source.mp4"
    shutil.copy2(video, destination)
    return destination.relative_to(workspace.root).as_posix()


def test_extract_frames_and_thumbnails_share_one_manifest(
    synthetic_video: Path,
    executor: ToolCallExecutor,
    workspace: Workspace,
) -> None:
    relative = _ingest(workspace, synthetic_video)
    executor.execute(
        ToolCall(
            seq=0,
            tool="extract_frames",
            args={
                "src": relative,
                "dst_dir": "frames/extracted",
                "fps": 1,
                "width": 320,
                "thumbs_dir": "frames/extracted/thumbs",
                "thumb_width": 160,
                "thumb_height": 90,
            },
        )
    )

    refs = frames.list_frames(
        workspace.resolve("frames/extracted"),
        workspace.root,
        1.0,
        thumbnail_dir=workspace.resolve("frames/extracted/thumbs"),
    )
    assert len(refs) >= 2
    assert all(ref.thumbnail for ref in refs)
    assert all(workspace.resolve(ref.thumbnail).is_file() for ref in refs)

    manifest = workspace.resolve("frames/extracted/manifest.json")
    frames.write_manifest(manifest, refs)
    assert not list(manifest.parent.glob("manifest.json.part-*"))
    loaded = frames.read_manifest(manifest)
    assert [item.model_dump(mode="json") for item in loaded] == [
        item.model_dump(mode="json") for item in refs
    ]
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload[0]["thumbnail"].endswith("thumb_000000.jpg")

    # 单次解码的缩略图可直接拼接触表，尺寸/数量保持一致。
    sheet = contactsheet.build_contact_sheet(
        refs,
        workspace.root,
        workspace.root / "frames" / "sheet.jpg",
        thumb_width=160,
        thumb_height=90,
    )
    with Image.open(workspace.root / sheet) as image:
        assert image.width > 0 and image.height > 0


def test_thumbnail_output_does_not_change_frame_list(
    synthetic_video: Path,
    executor: ToolCallExecutor,
    workspace: Workspace,
) -> None:
    relative = _ingest(workspace, synthetic_video)
    executor.execute(
        ToolCall(
            seq=0,
            tool="extract_frames",
            args={"src": relative, "dst_dir": "frames/plain", "fps": 1, "width": 320},
        )
    )
    executor.execute(
        ToolCall(
            seq=0,
            tool="extract_frames",
            args={
                "src": relative,
                "dst_dir": "frames/thumbs",
                "fps": 1,
                "width": 320,
                "thumbs_dir": "frames/thumbs/thumbs",
            },
        )
    )

    plain = frames.list_frames(
        workspace.resolve("frames/plain"), workspace.root, 1.0
    )
    combined = frames.list_frames(
        workspace.resolve("frames/thumbs"),
        workspace.root,
        1.0,
        thumbnail_dir=workspace.resolve("frames/thumbs/thumbs"),
    )
    assert [item.index for item in plain] == [item.index for item in combined]
    assert [item.t.to_float() for item in plain] == [
        item.t.to_float() for item in combined
    ]
    assert all(item.thumbnail is None for item in plain)
    assert all(item.thumbnail is not None for item in combined)
