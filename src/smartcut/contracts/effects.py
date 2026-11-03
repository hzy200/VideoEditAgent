"""特效契约：严格保持时长不变的视觉变换。

硬规则：
1. 特效只描述画面变换，不改变输出时长；变速/转场属于时间轴操作。
2. 参数只能是枚举与数值；禁止滤镜表达式、路径或像素坐标进入 IR。
3. L1 只支持全局特效（``target="output"``）；逐片段特效在 L2 打开。
"""

from __future__ import annotations

from collections import Counter
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from smartcut.contracts.ids import SafeId
from smartcut.contracts.time import RationalTime
from smartcut.errors import IRValidationError, UnsupportedFeature

_RATE = 90_000
MAX_OUTPUT_EFFECTS = 7
MAX_SEGMENT_EFFECTS = 7


def zero_time() -> RationalTime:
    return RationalTime(value=0, rate=_RATE)


class EffectBase(BaseModel):
    """所有特效的公共字段。``target=segment`` 需要显式 ``segment_id``。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Literal["output", "segment"] = "output"
    segment_id: SafeId | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def _target_consistent(self) -> "EffectBase":
        if self.target == "segment" and self.segment_id is None:
            raise ValueError("segment 特效必须提供 segment_id")
        if self.target == "output" and self.segment_id is not None:
            raise ValueError("output 特效不得携带 segment_id")
        return self


class FitEffect(EffectBase):
    """9:16 竖版适配；crop 由 Compiler 根据人脸锚点推导。"""

    type: Literal["fit"] = "fit"
    aspect: Literal["9:16"] = "9:16"
    anchor: Literal["face_bbox", "center"] = "face_bbox"


class ColorEffect(EffectBase):
    type: Literal["color"] = "color"
    brightness: float = Field(default=0.0, ge=-1.0, le=1.0)
    contrast: float = Field(default=1.0, ge=0.0, le=3.0)
    saturation: float = Field(default=1.0, ge=0.0, le=3.0)
    gamma: float = Field(default=1.0, ge=0.1, le=10.0)
    # 暖/冷偏移；Compiler 映射为开尔文色温（6500K 为中性）。
    temperature: int = Field(default=0, ge=-100, le=100)


class BlurEffect(EffectBase):
    type: Literal["blur"] = "blur"
    radius: float = Field(default=0.0, ge=0.0, le=50.0)


class SharpenEffect(EffectBase):
    type: Literal["sharpen"] = "sharpen"
    amount: float = Field(default=0.5, ge=0.0, le=2.0)


class VignetteEffect(EffectBase):
    type: Literal["vignette"] = "vignette"
    angle: float = Field(default=0.6, ge=0.0, le=1.57)


class FadeEffect(EffectBase):
    type: Literal["fade"] = "fade"
    fade_in: RationalTime = Field(default_factory=zero_time)
    fade_out: RationalTime = Field(default_factory=zero_time)

    @model_validator(mode="after")
    def _non_negative(self) -> "FadeEffect":
        if self.fade_in.as_fraction() < 0 or self.fade_out.as_fraction() < 0:
            raise ValueError("fade_in/fade_out 不能为负")
        return self


class ZoomEffect(EffectBase):
    type: Literal["zoom"] = "zoom"
    start_scale: float = Field(default=1.0, ge=1.0, le=2.0)
    end_scale: float = Field(default=1.1, ge=1.0, le=2.0)


Effect = Annotated[
    Union[
        FitEffect,
        ColorEffect,
        BlurEffect,
        SharpenEffect,
        VignetteEffect,
        FadeEffect,
        ZoomEffect,
    ],
    Field(discriminator="type"),
]

# 兼容旧导入名；新代码请直接使用 ``Effect`` 与其具体子类。
EffectSpec = Effect

EFFECT_MODELS: tuple[type[EffectBase], ...] = (
    FitEffect,
    ColorEffect,
    BlurEffect,
    SharpenEffect,
    VignetteEffect,
    FadeEffect,
    ZoomEffect,
)


def enabled_output_effects(effects: list[Effect]) -> list[Effect]:
    return [
        effect
        for effect in effects
        if effect.enabled and effect.target == "output"
    ]


def enabled_segment_effects(effects: list[Effect]) -> list[Effect]:
    return [
        effect
        for effect in effects
        if effect.enabled and effect.target == "segment"
    ]


def validate_effects(
    effects: list[Effect],
    *,
    total_duration: RationalTime,
    segment_durations: dict[str, RationalTime] | None = None,
) -> None:
    """校验全局与逐片段特效；``segment_durations`` 非空时同时校验引用完整性。"""
    enabled = [effect for effect in effects if effect.enabled]
    output = [effect for effect in enabled if effect.target == "output"]
    if len(output) > MAX_OUTPUT_EFFECTS:
        raise IRValidationError(
            f"全局特效最多 {MAX_OUTPUT_EFFECTS} 个，收到 {len(output)} 个"
        )

    for effect in enabled:
        if effect.target == "segment":
            if isinstance(effect, FitEffect):
                raise UnsupportedFeature("fit 只能是全局特效")
            if effect.segment_id is None:
                raise IRValidationError("逐片段特效必须提供 segment_id")
            if (
                segment_durations is not None
                and effect.segment_id not in segment_durations
            ):
                raise IRValidationError(
                    f"特效引用了不存在的片段: {effect.segment_id}"
                )

    counts = Counter(
        (
            effect.target,
            effect.segment_id or "",
            effect.type,
        )
        for effect in enabled
    )
    duplicated = sorted(
        f"{target}:{segment_id or '-'}:{kind}"
        for (target, segment_id, kind), count in counts.items()
        if count > 1
    )
    if duplicated:
        raise IRValidationError(
            "同一作用域内同类型特效最多一个，重复: " + ", ".join(duplicated)
        )

    per_segment = Counter(
        effect.segment_id
        for effect in enabled
        if effect.target == "segment" and effect.segment_id
    )
    over = [
        segment_id
        for segment_id, count in per_segment.items()
        if count > MAX_SEGMENT_EFFECTS
    ]
    if over:
        raise IRValidationError(
            f"单个片段最多 {MAX_SEGMENT_EFFECTS} 个特效，超出: {sorted(over)}"
        )

    for effect in enabled:
        if not isinstance(effect, FadeEffect):
            continue
        duration = total_duration
        if (
            effect.target == "segment"
            and segment_durations is not None
            and effect.segment_id in segment_durations
        ):
            duration = segment_durations[effect.segment_id]
        if (
            effect.fade_in.as_fraction() + effect.fade_out.as_fraction()
            > duration.as_fraction()
        ):
            raise IRValidationError(
                "fade_in + fade_out 不能超过目标时长 "
                f"({duration.to_ffmpeg_seconds()}s)"
            )


def effect_catalog_payload() -> dict:
    """给 Planner 的封闭特效目录；模型只能从这里选择类型与参数范围。"""
    return {
        "max_output_effects": 7,
        "types": [
            {
                "type": "fit",
                "params": {"aspect": ["9:16"], "anchor": ["face_bbox", "center"]},
            },
            {
                "type": "color",
                "params": {
                    "brightness": [-1.0, 1.0, 0.0],
                    "contrast": [0.0, 3.0, 1.0],
                    "saturation": [0.0, 3.0, 1.0],
                    "gamma": [0.1, 10.0, 1.0],
                    "temperature": [-100, 100, 0],
                },
            },
            {"type": "blur", "params": {"radius": [0.0, 50.0, 0.0]}},
            {"type": "sharpen", "params": {"amount": [0.0, 2.0, 0.5]}},
            {"type": "vignette", "params": {"angle": [0.0, 1.57, 0.6]}},
            {
                "type": "fade",
                "params": {
                    "fade_in": "RationalTime",
                    "fade_out": "RationalTime",
                },
            },
            {
                "type": "zoom",
                "params": {"start_scale": [1.0, 2.0, 1.0], "end_scale": [1.0, 2.0, 1.1]},
            },
        ],
    }
