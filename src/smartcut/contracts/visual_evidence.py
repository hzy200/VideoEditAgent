"""视觉证据契约：把视频变化翻译成可复现、可定位、可拒绝的视图。

硬规则：
- 证据只描述候选区间（``time_role=candidate_only``），不得直接当作剪切点；
- 所有时间用 ``RationalTime``，哈希使用 canonical JSON；
- ``render_hash`` 不含 backend/driver/耗时，跨后端只比较 ``semantic_hash``。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from smartcut.contracts.time import RationalTime

ViewKind = Literal[
    "contact_sheet",
    "temporal_diff",
    "flow_strip",
    "codec_residual",
    "bbox_overlay",
    "roi_crop",
]

LABEL_RE = re.compile(r"^T\d{2,3}$")

SCHEMA_VERSION = "1.0"
TIME_ROLE = "candidate_only"


class Tile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=1)
    label: str = Field(pattern=LABEL_RE.pattern)
    t: RationalTime
    pts: int
    frame_no: int | None = Field(default=None, ge=0)
    row: int = Field(default=0, ge=0)
    col: int = Field(default=0, ge=0)
    frame_ref: str | None = Field(
        default=None,
        description="工作区相对路径，仅用于回溯原帧，不参与渲染",
    )


class TileMap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    map_id: str = Field(min_length=1, max_length=120)
    content_id: str = Field(min_length=1, max_length=120)
    time_base: str = Field(default="1/1", min_length=1, max_length=32)
    vfr: bool = False
    origin_pts: int = 0
    tiles: list[Tile] = Field(min_length=1)

    @model_validator(mode="after")
    def _contiguous(self) -> "TileMap":
        indices = [tile.index for tile in self.tiles]
        if indices != list(range(1, len(self.tiles) + 1)):
            raise ValueError("tiles.index 必须从 1 开始连续递增")
        labels = [tile.label for tile in self.tiles]
        if len(labels) != len(set(labels)):
            raise ValueError("tiles.label 不能重复")
        return self


class Measurement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tile_index: int = Field(ge=1)
    d_hist: float = Field(default=0.0, ge=0.0, le=1.0)
    flow_mag: float | None = Field(default=None, ge=0.0)
    flow_dir_deg: float | None = None
    bbox_xywh: tuple[int, int, int, int] | None = None
    width: int = Field(default=0, ge=0)
    height: int = Field(default=0, ge=0)
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    engine: str = "cpu"


class MeasureResult(BaseModel):
    """CPU 测量结果；renderer 只接收这里的数据，不得访问原始素材。"""

    model_config = ConfigDict(extra="forbid")

    tile_map: TileMap
    tile_images: list[bytes] = Field(min_length=1)
    measurements: list[Measurement] = Field(min_length=1)
    overlays: list[dict[str, Any]] = Field(default_factory=list)
    constants: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _aligned(self) -> "MeasureResult":
        count = len(self.tile_map.tiles)
        if len(self.tile_images) != count or len(self.measurements) != count:
            raise ValueError("tile_images / measurements 必须与 TileMap 等长")
        return self


class RenderedImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    png_bytes: bytes = Field(min_length=1)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    backend: str = Field(min_length=1)
    shader_hash: str = Field(min_length=1)
    device_tier: str = "unknown"


class ViewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ViewKind = "contact_sheet"
    content_id: str = Field(min_length=1, max_length=120)
    source_range: tuple[RationalTime, RationalTime]
    top_k: int = Field(default=7, ge=1, le=64)
    grid_cols: int = Field(default=4, ge=1, le=8)
    tile_width: int = Field(default=384, ge=16, le=2048)
    tile_height: int = Field(default=216, ge=16, le=2048)
    label_format: str = Field(default="T%02d", min_length=2, max_length=16)
    policy_version: str = Field(default="views@1.0", min_length=1, max_length=64)


class VisualEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    kind: ViewKind
    source_range: tuple[RationalTime, RationalTime]
    source_ref: dict[str, Any]
    tile_map: TileMap
    measurements: list[Measurement]
    image_ref: str = Field(min_length=1)
    meta: dict[str, Any]
    confidence: float = Field(ge=0.0, le=1.0)
    time_role: Literal["candidate_only"] = TIME_ROLE
    render_hash: str = Field(min_length=1)
    semantic_hash: str = Field(min_length=1)
    fallback_of: str | None = None
    ir_revision: int | None = None


REQUIRED_META_KEYS = frozenset(
    {
        "contract_version",
        "policy_version",
        "ffmpeg_version",
        "opencv_version",
        "fps_sample",
        "dedup_threshold",
        "scene_threshold_pct",
        "color_convention",
        "yuv_to_rgb_matrix_id",
        "tile_label_format",
        "grid_rows",
        "grid_cols",
        "tile_w",
        "tile_h",
        "pad_px",
        "shader_hash",
        "backend",
        "device_tier",
    }
)
HASH_EXCLUDED_META_KEYS = frozenset({"backend", "device_tier"})


def _normalize(value: Any) -> Any:
    if isinstance(value, RationalTime):
        return f"{value.value}/{value.rate}"
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {
            str(key): _normalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("哈希输入不允许 NaN/Infinity")
        return round(value, 6)
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(value: Any) -> str:
    if isinstance(value, bytes):
        payload = value
    else:
        payload = canonical_bytes(value)
    return hashlib.sha256(payload).hexdigest()


def stable_hash(value: Any) -> str:
    return f"sha256:{sha256_hex(value)}"


def tile_map_hash(tile_map: TileMap | dict) -> str:
    return sha256_hex(tile_map)


def measurements_hash(measurements: list[Measurement] | list[dict]) -> str:
    return sha256_hex(measurements)


def stable_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in meta.items()
        if key not in HASH_EXCLUDED_META_KEYS
    }


def compute_render_hash(
    *,
    schema_version: str,
    kind: str,
    media_sha256_16: str,
    tile_map_sha: str,
    measurements_sha: str,
    image_bytes_sha: str,
    meta: dict[str, Any],
) -> str:
    return stable_hash(
        {
            "schema_version": schema_version,
            "kind": kind,
            "media_sha256_16": media_sha256_16,
            "tile_map_hash": tile_map_sha,
            "measurements_hash": measurements_sha,
            "image_bytes_sha256": image_bytes_sha,
            "stable_meta": stable_meta(meta),
        }
    )


def evidence_id(
    content_id: str,
    kind: str,
    source_range: tuple[RationalTime, RationalTime],
) -> str:
    digest = sha256_hex(
        {
            "content_id": content_id,
            "kind": kind,
            "start": source_range[0],
            "end": source_range[1],
        }
    )[:10]
    short = re.sub(r"[^A-Za-z0-9_-]", "", content_id)[:12] or "content"
    return f"ve_{short}_{kind}_{digest}"


def tile_map_id(content_id: str, tiles: list[Tile]) -> str:
    digest = sha256_hex(
        {
            "content_id": content_id,
            "tiles": [
                {"pts": tile.pts, "label": tile.label, "t": tile.t}
                for tile in tiles
            ],
        }
    )[:10]
    short = re.sub(r"[^A-Za-z0-9_-]", "", content_id)[:10] or "content"
    return f"tm_{short}_{digest}"


def validate_meta(meta: dict[str, Any]) -> None:
    missing = REQUIRED_META_KEYS - set(meta)
    if missing:
        raise ValueError(f"证据 meta 缺少必填常量: {sorted(missing)}")
