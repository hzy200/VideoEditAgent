"""有理数时间。

规约：
- IR 内只允许 ``RationalTime(value, rate)``，表示 ``value / rate`` 秒。
- 任何算术都通过 ``Fraction`` 完成，禁止浮点秒参与时间轴运算。
- 仅在生成 ffmpeg argv 时才转成十进制秒字符串。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


def _round_half_up(value: Fraction) -> int:
    """对有理数做四舍五入（非 Python 的银行家舍入）。"""
    return int((value + Fraction(1, 2)).__floor__())


def frame_tolerance(fps: Fraction | None, frames: int = 2) -> Fraction:
    """允许的帧级误差：无法确定帧率时回退到 50ms。"""
    if fps is None or fps <= 0:
        return Fraction(1, 20)
    return Fraction(frames, 1) / fps


def rt_from_seconds(seconds: float, *, rate: int = 90_000) -> RationalTime:
    """从浮点秒构造非负 RationalTime；感知层边界的唯一入口。"""
    return RationalTime.from_seconds(max(0.0, seconds), rate=rate)


def rt_to_dict(value: RationalTime) -> dict[str, int]:
    """转成工具参数里的 ``{"value": ..., "rate": ...}`` 结构。"""
    return {"value": value.value, "rate": value.rate}


def seconds_from_mapping(value: Any) -> float | None:
    """解析 ``{"value", "rate"}``；无法解析时返回 None。"""
    if not isinstance(value, dict):
        return None
    rate = value.get("rate")
    if not rate:
        return None
    try:
        return float(value["value"]) / float(rate)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def fps_from_string(text: str | int | float | Fraction) -> Fraction:
    """解析 ffprobe 的帧率字符串，如 ``"30000/1001"``、``"30"``。"""
    try:
        if isinstance(text, Fraction):
            fps = text
        elif isinstance(text, (int, float)):
            fps = Fraction(str(text))
        else:
            raw = text.strip()
            if "/" in raw:
                numerator, denominator = raw.split("/", 1)
                fps = Fraction(int(numerator), int(denominator))
            else:
                fps = Fraction(raw)
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise ValueError(f"非法帧率: {text!r}") from exc
    if fps <= 0:
        raise ValueError(f"非法帧率: {text!r}")
    return fps


def rational_to_string(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


class RationalTime(BaseModel):
    """``value / rate`` 秒。rate 必须为正整数。"""

    model_config = ConfigDict(frozen=True)

    value: int
    rate: int

    @field_validator("rate")
    @classmethod
    def _positive_rate(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("rate 必须为正整数")
        return value

    # ── 构造 ──────────────────────────────────────────────────────────
    @classmethod
    def from_fraction(cls, value: Fraction) -> "RationalTime":
        return cls(value=value.numerator, rate=value.denominator)

    @classmethod
    def from_seconds(
        cls,
        seconds: str | int | float | Decimal,
        rate: int = 90_000,
    ) -> "RationalTime":
        """把秒数换算到指定整数时基，四舍五入到最近 tick。"""
        if rate <= 0:
            raise ValueError("rate 必须为正整数")
        # numpy.float64 / numpy.int64 等标量先转成 Python 标量，避免 repr 形式无法解析。
        if hasattr(seconds, "item") and not isinstance(seconds, (str, bytes)):
            try:
                seconds = seconds.item()
            except (ValueError, AttributeError):
                pass
        if isinstance(seconds, int):
            return cls(value=seconds * rate, rate=rate)
        if isinstance(seconds, float):
            text = repr(seconds)
        elif isinstance(seconds, Decimal):
            text = str(seconds)
        else:
            text = str(seconds).strip()
        try:
            seconds_decimal = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"无法解析时间: {seconds!r}") from exc
        ticks = (seconds_decimal * rate).to_integral_value(rounding=ROUND_HALF_UP)
        return cls(value=int(ticks), rate=rate)

    @classmethod
    def from_frames(cls, frames: int, fps: Fraction) -> "RationalTime":
        """第 ``frames`` 帧的精确时间：``frames / fps``。"""
        if fps <= 0:
            raise ValueError("fps 必须为正数")
        return cls(value=frames * fps.denominator, rate=fps.numerator)

    # ── 转换 ──────────────────────────────────────────────────────────
    def as_fraction(self) -> Fraction:
        return Fraction(self.value, self.rate)

    def to_float(self) -> float:
        """仅供展示或与 ffprobe 浮点值比较；不要用于时间轴运算。"""
        return self.value / self.rate

    def to_ffmpeg_seconds(self, precision: int = 9) -> str:
        seconds = Decimal(self.value) / Decimal(self.rate)
        quantum = Decimal(1).scaleb(-precision)
        rounded = seconds.quantize(quantum, rounding=ROUND_HALF_UP)
        text = format(rounded, "f").rstrip("0").rstrip(".")
        return text or "0"

    def round_to_rate(self, rate: int) -> "RationalTime":
        if rate <= 0:
            raise ValueError("rate 必须为正整数")
        return RationalTime(value=_round_half_up(self.as_fraction() * rate), rate=rate)

    def snap_to_frame(self, fps: Fraction) -> "RationalTime":
        """吸附到最近的整帧；仅在显式调用处使用，编译器不做隐式吸附。"""
        if fps <= 0:
            raise ValueError("fps 必须为正数")
        frame_index = _round_half_up(self.as_fraction() * fps)
        return RationalTime.from_frames(frame_index, fps)

    def frames(self, fps: Fraction) -> Fraction:
        return self.as_fraction() * fps

    # ── 比较与算术 ────────────────────────────────────────────────────
    def __add__(self, other: "RationalTime") -> "RationalTime":
        if not isinstance(other, RationalTime):
            return NotImplemented
        return RationalTime.from_fraction(self.as_fraction() + other.as_fraction())

    def __sub__(self, other: "RationalTime") -> "RationalTime":
        if not isinstance(other, RationalTime):
            return NotImplemented
        return RationalTime.from_fraction(self.as_fraction() - other.as_fraction())

    def __lt__(self, other: "RationalTime") -> bool:
        return self.as_fraction() < other.as_fraction()

    def __le__(self, other: "RationalTime") -> bool:
        return self.as_fraction() <= other.as_fraction()

    def __gt__(self, other: "RationalTime") -> bool:
        return self.as_fraction() > other.as_fraction()

    def __ge__(self, other: "RationalTime") -> bool:
        return self.as_fraction() >= other.as_fraction()

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, RationalTime):
            return self.as_fraction() == other.as_fraction()
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.as_fraction())

    def __str__(self) -> str:
        return f"{self.value}/{self.rate}s"


def sum_rational_times(values: list[RationalTime]) -> RationalTime:
    total = sum((item.as_fraction() for item in values), Fraction(0))
    return RationalTime.from_fraction(total)
