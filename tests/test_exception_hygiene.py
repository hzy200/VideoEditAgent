from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "smartcut"
BROAD = {"Exception", "BaseException"}


def _is_broad(handler: ast.ExceptHandler) -> bool:
    node = handler.type
    if isinstance(node, ast.Name):
        return node.id in BROAD
    if isinstance(node, ast.Tuple):
        return any(
            isinstance(item, ast.Name) and item.id in BROAD for item in node.elts
        )
    return False


def test_no_broad_except_handler_is_silently_passed() -> None:
    """`except Exception: pass` 会让回退路径不可观测，禁止回潮。"""
    violations: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or not _is_broad(node):
                continue
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


def test_broad_except_handlers_perform_an_observable_action() -> None:
    """宽泛捕获至少要有日志/返回/抛出/信号/结果写入之一。"""
    observable = (
        ast.Return,
        ast.Raise,
        ast.Continue,
        ast.Break,
        ast.Assign,
        ast.AnnAssign,
        ast.AugAssign,
        ast.Expr,
    )
    violations: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or not _is_broad(node):
                continue
            if not any(isinstance(item, observable) for item in node.body):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []
