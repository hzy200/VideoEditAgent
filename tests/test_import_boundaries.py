from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "smartcut"

# 各层允许依赖的 smartcut 子包。基础设施（config/errors/logging/resources/cancel）
# 不参与领域分层；新增边必须先在这里登记并在 docs/INVARIANTS.md 说明理由。
ALLOWED_EDGES: dict[str, set[str]] = {
    "contracts": {"errors"},
    # ocr_worker 是独立进程入口；重依赖只在 worker 内懒加载。
    "perception": {"contracts", "config", "errors", "logging", "ocr_worker"},
    "compiler": {"contracts", "errors"},
    # planner -> llm 是 DeepSeek 客户端的直接依赖，属于已登记边。
    "planner": {"contracts", "config", "errors", "llm", "logging", "resources"},
    "llm": {"config", "errors", "logging", "resources"},
    # runner 可复用 perception 的纯解析函数（见 AGENTS.md 分层铁律 6）。
    "runner": {
        "cancel",
        "config",
        "contracts",
        "errors",
        "logging",
        "perception",
        "policy",
        "resources",
    },
    "orchestrator": {"config", "contracts", "errors", "llm", "logging", "policy"},
    "server": {
        "cancel",
        "compiler",
        "config",
        "contracts",
        "errors",
        "llm",
        "logging",
        "orchestrator",
        "perception",
        "planner",
        "policy",
        "resources",
        "runner",
        "workspace",
    },
    "desktop": {
        "cancel",
        "config",
        "contracts",
        "logging",
        "perception",
        "policy",
        "runner",
        "server",
    },
}

def _smartcut_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return {
        module
        for module in modules
        if module == "smartcut" or module.startswith("smartcut.")
    }


def test_package_dependencies_stay_within_registered_edges() -> None:
    violations: list[str] = []
    for package, allowed in ALLOWED_EDGES.items():
        for path in (SRC / package).rglob("*.py"):
            for module in _smartcut_imports(path):
                parts = module.split(".")
                if len(parts) < 2:
                    continue
                target = parts[1]
                if target != package and target not in allowed:
                    violations.append(
                        f"{path.relative_to(ROOT)}: {package} -> {target}"
                    )
    assert violations == []


def test_gui_files_do_not_touch_backend_or_subprocess() -> None:
    paths = [SRC / "desktop" / "main_window.py"]
    paths.extend((SRC / "desktop" / "widgets").glob("*.py"))
    violations: list[str] = []
    for path in paths:
        for module in _smartcut_imports(path):
            if any(
                module == prefix or module.startswith(prefix + ".")
                for prefix in ("smartcut.server", "smartcut.runner")
            ):
                violations.append(f"{path.relative_to(ROOT)}: {module}")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if name.split(".")[0] in {"subprocess", "httpx", "requests"}:
                    violations.append(f"{path.relative_to(ROOT)}: {name}")
    assert violations == []
