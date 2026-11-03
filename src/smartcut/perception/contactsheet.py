"""Contact sheet：Pillow 拼图，供 GUI 展示与后续多图理解。"""

from __future__ import annotations

from pathlib import Path

from smartcut.contracts.perception import FrameRef
from smartcut.errors import PerceptionUnavailable


def build_contact_sheet(
    frames: list[FrameRef],
    workspace_root: Path,
    output_path: Path,
    columns: int = 4,
    thumb_width: int = 320,
    thumb_height: int = 180,
    max_frames: int = 12,
) -> str:
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps
    except ImportError as exc:  # pragma: no cover
        raise PerceptionUnavailable("需要 Pillow；pip install -e '.[vision]'") from exc

    if not frames:
        raise PerceptionUnavailable("没有可用的抽帧")

    if len(frames) > max_frames:
        step = max(1, len(frames) // max_frames)
        selected = frames[::step][:max_frames]
    else:
        selected = frames

    rows = (len(selected) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * thumb_width, rows * thumb_height), "#111827")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()

    for index, frame in enumerate(selected):
        # 优先使用同一次抽帧解码产出的缩略图，避免重复读取大图。
        source = workspace_root / (frame.thumbnail or frame.path)
        if not source.is_file():
            source = workspace_root / frame.path
        image = Image.open(source).convert("RGB")
        thumbnail = ImageOps.fit(
            image,
            (thumb_width, thumb_height),
            method=Image.Resampling.LANCZOS,
        )
        x = (index % columns) * thumb_width
        y = (index // columns) * thumb_height
        sheet.paste(thumbnail, (x, y))
        label = f"{frame.t.to_ffmpeg_seconds()}s"
        draw.rectangle(
            (x + 4, y + thumb_height - 22, x + 88, y + thumb_height - 4),
            fill="#0f172a",
        )
        draw.text((x + 8, y + thumb_height - 20), label, fill="#f8fafc", font=font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, format="JPEG", quality=85)
    return output_path.relative_to(workspace_root).as_posix()
