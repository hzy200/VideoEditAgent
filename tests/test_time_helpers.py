from __future__ import annotations

import ast
from pathlib import Path

from smartcut.contracts.time import RationalTime, rt_from_seconds, rt_to_dict
from smartcut.desktop.formatting import format_seconds, seconds_or_none

ROOT = Path(__file__).resolve().parents[1]


def test_rt_helpers_round_trip() -> None:
    value = rt_from_seconds(1.5)
    assert value.to_float() == 1.5
    assert rt_to_dict(value) == {"value": 135_000, "rate": 90_000}
    assert rt_from_seconds(-1).to_float() == 0.0


def test_desktop_seconds_helpers() -> None:
    assert seconds_or_none({"value": 1500, "rate": 1000}) == 1.5
    assert seconds_or_none(2) == 2.0
    assert seconds_or_none({"rate": 0}) is None
    assert seconds_or_none("1.5") is None
    assert format_seconds({"value": 1, "rate": 1000}) == "0.001"
    assert format_seconds(None) == "-"


def test_no_local_rt_or_seconds_duplicates() -> None:
    offenders: list[str] = []
    for path in (ROOT / "src" / "smartcut").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in {"_rt", "_seconds"}:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []
