from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PIL import Image

from smartcut.contracts.perception import FrameRef
from smartcut.contracts.time import RationalTime
from smartcut.contracts.tools import ToolCall
from smartcut.perception import contactsheet, faces, frames, quality, scenes
from smartcut.runner.execute import ToolCallExecutor
from smartcut.runner.paths import Workspace


def _make_multi_scene(tmp_path: Path, ffmpeg_bin: str) -> Path:
    clips: list[Path] = []
    for index, color in enumerate(("red", "black", "blue", "green")):
        clip = tmp_path / f"clip_{index}.mp4"
        subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=320x240:d=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "30",
                str(clip),
            ],
            check=True,
            capture_output=True,
        )
        clips.append(clip)
    listing = tmp_path / "concat.txt"
    listing.write_text(
        "\n".join(f"file '{clip.as_posix()}'" for clip in clips) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "multi.mp4"
    subprocess.run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c",
            "copy",
            str(output),
        ],
        check=True,
        capture_output=True,
    )
    return output


def test_open_cv_perception_pipeline(
    tmp_path: Path,
    ffmpeg_bin: str,
    executor: ToolCallExecutor,
    workspace: Workspace,
) -> None:
    video = _make_multi_scene(tmp_path, ffmpeg_bin)
    destination = workspace.media_dir / "multi.mp4"
    shutil.copy2(video, destination)
    relative = destination.relative_to(workspace.root).as_posix()

    executor.execute(
        ToolCall(
            seq=0,
            tool="extract_frames",
            args={
                "src": relative,
                "dst_dir": "frames/extracted",
                "fps": 1,
                "width": 320,
            },
        )
    )
    refs = frames.list_frames(
        workspace.resolve("frames/extracted"),
        workspace.root,
        1.0,
    )
    assert len(refs) >= 3

    shot_list = scenes.detect_scenes(workspace.resolve_input(relative), relative)
    assert len(shot_list.shots) >= 2

    report = quality.analyze_quality(refs, workspace.root, relative, 1.0)
    assert any(mark.kind.value == "black" for mark in report.marks)

    parallel = quality.analyze_quality(
        refs,
        workspace.root,
        relative,
        1.0,
        max_workers=2,
    )
    assert [(mark.t, mark.kind) for mark in parallel.marks] == [
        (mark.t, mark.kind) for mark in report.marks
    ]

    face_report = faces.detect_faces(refs, workspace.root, relative, 1.0)
    assert face_report.anchors == []
    parallel_faces = faces.detect_faces(
        refs,
        workspace.root,
        relative,
        1.0,
        max_workers=2,
    )
    assert parallel_faces.anchors == []

    contact_rel = contactsheet.build_contact_sheet(
        refs,
        workspace.root,
        workspace.root / "frames" / "contact_sheet.jpg",
    )
    contact_path = workspace.root / contact_rel
    assert contact_path.exists()
    with Image.open(contact_path) as image:
        assert image.width > 0 and image.height > 0


def test_analysis_frame_sampling_keeps_scene_cuts() -> None:
    refs = [
        FrameRef(
            index=index,
            t=RationalTime.from_seconds(str(index), rate=90_000),
            path=f"frames/{index}.jpg",
        )
        for index in range(100)
    ]
    selected = frames.select_analysis_frames(refs, [50.0], max_frames=10)
    assert len(selected) == 10
    positions = [item.index for item in selected]
    assert positions == sorted(positions)
    assert 49 in positions or 50 in positions

    unlimited = frames.select_analysis_frames(refs, [50.0], max_frames=0)
    assert unlimited is refs
