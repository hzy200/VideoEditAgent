"""文档中反引号引用的源码路径必须真实存在，防止重构后文档漂移。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE_SUFFIXES = {
    ".py",
    ".json",
    ".md",
    ".sql",
    ".spec",
    ".toml",
    ".yml",
    ".yaml",
}
# 运行期数据文件，不是仓库源码。
RUNTIME_NAMES = {
    "desktop.json",
    "result.json",
    "manifest.json",
    "smartcut.jsonl",
    "scenes.json",
    "quality.json",
    "faces.json",
    "progress.json",
    "evidence.json",
}
# 前瞻设计稿允许引用尚未落地的目标文件，不做存在性断言。
PROPOSAL_DOCS = {"P1_MULTI_SOURCE.md"}
DIRECT_ROOTS = (
    ROOT,
    ROOT / "src" / "smartcut",
    ROOT / "docs",
    ROOT / "tests",
    ROOT / "scripts",
    ROOT / "packaging",
)
DOC_FILES = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "智能剪辑系统技术方案.md",
    *sorted((ROOT / "docs").glob("*.md")),
]
TOKEN_RE = re.compile(r"`([^`\n]+)`")


def _clean_token(token: str) -> str:
    token = token.split("::", 1)[0].strip()
    token = token.strip("'\"")
    token = re.sub(r"\([^)]*\)$", "", token).strip()
    return token.rstrip("；。，、,.;:：)）]】")


def _file_index() -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for base in (
        ROOT / "src" / "smartcut",
        ROOT / "tests",
        ROOT / "scripts",
        ROOT / "packaging",
        ROOT / "docs",
    ):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in CODE_SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            index.setdefault(path.name, []).append(path)
    return index


def _is_source_token(token: str) -> bool:
    if not token or token.startswith(("http://", "https://")):
        return False
    if "\\" in token or token.startswith("/") or "=" in token:
        return False
    if any(char in token for char in "*?{}<>| "):
        return False
    return Path(token).suffix in CODE_SUFFIXES


def _resolve(token: str, index: dict[str, list[Path]]) -> bool:
    for base in DIRECT_ROOTS:
        if (base / token).is_file():
            return True
    name = Path(token).name
    matches = index.get(name, [])
    if not matches:
        return False
    if "/" not in token:
        return True
    suffix = "/" + token.lstrip("./")
    return any(
        path.as_posix().endswith(suffix) for path in matches
    )


def test_documented_source_paths_exist() -> None:
    index = _file_index()
    missing: list[str] = []
    for doc in DOC_FILES:
        if not doc.exists() or doc.name in PROPOSAL_DOCS:
            continue
        text = doc.read_text(encoding="utf-8")
        for raw in TOKEN_RE.findall(text):
            token = _clean_token(raw)
            if Path(token).name in RUNTIME_NAMES:
                continue
            if not _is_source_token(token):
                continue
            if not _resolve(token, index):
                missing.append(f"{doc.relative_to(ROOT).as_posix()}: {token}")
    assert not missing, "文档引用了不存在的源码路径：\n" + "\n".join(
        sorted(set(missing))
    )
