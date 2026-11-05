"""证据视图路由：按目标与感知信号选择 1 个主视图 + 最多 2 个补充视图。

纯函数、确定性，不抽帧、不调用模型、不写时间线。策略规则由调用方注入，
因此 ``perception`` 不依赖 ``policy`` 包。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RouteRule:
    id: str
    primary: str
    extras: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoutingRules:
    version: str
    default: str
    types: tuple[RouteRule, ...]
    face_coverage_threshold: float = 0.3
    shot_rate_threshold: float = 0.5
    transcript_chars_per_second_threshold: float = 2.0

    def by_id(self, type_id: str) -> RouteRule | None:
        return next((item for item in self.types if item.id == type_id), None)

    def default_rule(self) -> RouteRule:
        rule = self.by_id(self.default)
        if rule is None:
            raise ValueError(f"routing.default 不在 types 中: {self.default!r}")
        return rule


@dataclass(frozen=True)
class ViewRoute:
    content_type: str
    primary: str
    extras: tuple[str, ...]
    rationale: tuple[str, ...]
    policy_version: str

    @property
    def kinds(self) -> tuple[str, ...]:
        return (self.primary, *self.extras)


def rules_from_policy(routing: Any) -> RoutingRules | None:
    """把 ``policy.ViewRoutingPolicy`` 适配为路由层可用的纯数据。"""
    if routing is None:
        return None
    return RoutingRules(
        version=str(routing.version),
        default=str(routing.default),
        types=tuple(
            RouteRule(
                id=str(item.id),
                primary=str(item.primary),
                extras=tuple(str(kind) for kind in item.extras[:2]),
                keywords=tuple(str(word) for word in item.keywords),
            )
            for item in routing.types
        ),
        face_coverage_threshold=float(routing.face_coverage_threshold),
        shot_rate_threshold=float(routing.shot_rate_threshold),
        transcript_chars_per_second_threshold=float(
            routing.transcript_chars_per_second_threshold
        ),
    )


def _items(payload: Any, key: str) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    value = payload.get(key)
    return list(value or []) if isinstance(value, list) else []


def _frame_count(perception: dict | None) -> int:
    frames = (perception or {}).get("frame_manifest") or []
    return len(frames) if isinstance(frames, list) else 0


def _face_coverage(perception: dict | None, frame_count: int) -> tuple[float, int]:
    anchors = _items((perception or {}).get("faces"), "anchors")
    if frame_count <= 0:
        return (0.0, len(anchors))
    return (min(1.0, len(anchors) / frame_count), len(anchors))


def _shot_rate(perception: dict | None, duration_seconds: float) -> tuple[float, int]:
    shots = _items(perception.get("shots"), "shots")
    if duration_seconds <= 0:
        return (0.0, len(shots))
    return (len(shots) / duration_seconds, len(shots))


def _transcript_chars_per_second(transcript: Any, duration_seconds: float) -> float:
    if transcript is None or duration_seconds <= 0:
        return 0.0
    chars = sum(
        len(str(segment.text).strip())
        for segment in getattr(transcript, "segments", []) or []
    )
    return chars / duration_seconds


def _keyword_rule(goal: str, rules: RoutingRules) -> tuple[RouteRule, str] | None:
    text = (goal or "").strip().lower()
    if not text:
        return None
    for rule in rules.types:
        for keyword in rule.keywords:
            if keyword and keyword.lower() in text:
                return rule, f"goal:{keyword}"
    return None


def _signal_rule(
    rules: RoutingRules,
    *,
    face_coverage: float,
    face_count: int,
    shot_rate: float,
    shot_count: int,
    frame_count: int,
    transcript_cps: float,
) -> tuple[RouteRule, str] | None:
    if face_count > 0 and face_coverage >= rules.face_coverage_threshold:
        rule = rules.by_id("talking_head")
        if rule is not None:
            return (
                rule,
                f"face_coverage={face_coverage:.2f}>={rules.face_coverage_threshold:.2f}",
            )
    if shot_count > 0 and shot_rate >= rules.shot_rate_threshold:
        rule = rules.by_id("motion_demo")
        if rule is not None:
            return (
                rule,
                f"shot_rate={shot_rate:.2f}/s>={rules.shot_rate_threshold:.2f}",
            )
    if (
        frame_count > 0
        and transcript_cps < rules.transcript_chars_per_second_threshold
    ):
        rule = rules.by_id("screen_content")
        if rule is not None:
            return (
                rule,
                "transcript_cps="
                f"{transcript_cps:.2f}<{rules.transcript_chars_per_second_threshold:.2f}",
            )
    return None


def route_views(
    goal: str,
    perception: dict | None,
    transcript: Any | None,
    duration_seconds: float,
    rules: RoutingRules | None,
) -> ViewRoute:
    """选择证据视图组合；``rules`` 为空时回退单一 contact_sheet。"""
    if rules is None:
        return ViewRoute(
            content_type="unrouted",
            primary="contact_sheet",
            extras=(),
            rationale=("routing policy unavailable",),
            policy_version="unrouted",
        )

    frame_count = _frame_count(perception)
    face_coverage, face_count = _face_coverage(perception, frame_count)
    shot_rate, shot_count = _shot_rate(perception, duration_seconds)
    transcript_cps = _transcript_chars_per_second(transcript, duration_seconds)

    matched = _keyword_rule(goal, rules)
    rationale: list[str] = []
    if matched is not None:
        rule, reason = matched
        rationale.append(reason)
    else:
        signalled = _signal_rule(
            rules,
            face_coverage=face_coverage,
            face_count=face_count,
            shot_rate=shot_rate,
            shot_count=shot_count,
            frame_count=frame_count,
            transcript_cps=transcript_cps,
        )
        if signalled is not None:
            rule, reason = signalled
            rationale.append(reason)
        else:
            rule = rules.default_rule()
            rationale.append(
                f"default (face={face_coverage:.2f}, shot_rate={shot_rate:.2f}/s, "
                f"transcript_cps={transcript_cps:.2f})"
            )

    return ViewRoute(
        content_type=rule.id,
        primary=rule.primary,
        extras=tuple(rule.extras[:2]),
        rationale=tuple(rationale),
        policy_version=rules.version,
    )
