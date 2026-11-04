"""评估规则/LLM 选段质量：人工 EDL vs 生成的 EditIR。

用法：
    python scripts/evaluate_selection.py --edl tests/golden/edl/xxx.json \
        --source path/to/video.mp4 --mode rule --min-f1 0.9

LLM 模式需要 ``SMARTCUT_LIVE_DEEPSEEK=1`` 与 DEEPSEEK_API_KEY；默认只在
CI 跑规则模式。脚本自身不拼 ffmpeg 命令，媒体事实统一通过 Runner probe。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from smartcut.config import load_settings
from smartcut.contracts.workspace import Workspace
from smartcut.planner.client import DeepSeekPlanner
from smartcut.planner.evaluation import evaluate_selection, ir_intervals, load_edl
from smartcut.planner.hybrid import HybridPlanner
from smartcut.planner.rules import RulePlanner
from smartcut.runner.execute import ToolCallExecutor


def _probe(source: Path):
    settings = load_settings()
    root = Path(tempfile.mkdtemp(prefix="smartcut-eval-"))
    workspace = Workspace(root)
    for name in ("media", "audio", "proxy", "frames", "renders", "tmp", "logs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    destination = workspace.media_dir / f"source{source.suffix or '.mp4'}"
    shutil.copy2(source, destination)
    executor = ToolCallExecutor(settings, workspace)
    facts = executor.probe(destination.relative_to(root).as_posix())
    return settings, facts


def _build_planner(mode: str, settings):
    if mode == "rule":
        return RulePlanner(settings)
    if mode == "hybrid":
        return HybridPlanner(settings)
    planner = DeepSeekPlanner(settings)
    if not planner.available:
        raise SystemExit("LLM 模式需要 DEEPSEEK_API_KEY")
    return planner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SmartCut 选段质量评估")
    parser.add_argument("--edl", required=True, help="人工 EDL JSON")
    parser.add_argument("--source", required=True, help="源视频路径")
    parser.add_argument(
        "--mode",
        choices=("rule", "llm", "hybrid"),
        default="rule",
    )
    parser.add_argument("--output", help="指标 JSON 输出路径")
    parser.add_argument("--min-f1", type=float, default=None)
    args = parser.parse_args(argv)
    if args.mode in {"llm", "hybrid"} and os.getenv(
        "SMARTCUT_LIVE_DEEPSEEK"
    ) != "1":
        print(
            "LLM 模式需显式设置 SMARTCUT_LIVE_DEEPSEEK=1",
            file=sys.stderr,
        )
        return 2

    source = Path(args.source)
    if not source.is_file():
        print(f"源文件不存在: {source}", file=sys.stderr)
        return 2
    edl = load_edl(args.edl)
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    digest = digest.hexdigest()
    expected_hash = str(edl.get("source_hash") or "")
    if (
        re.fullmatch(r"[0-9a-fA-F]{16,64}", expected_hash)
        and expected_hash.lower() != digest
    ):
        print(
            f"警告：EDL source_hash 与素材不一致（{expected_hash[:12]}…）",
            file=sys.stderr,
        )

    settings, facts = _probe(source)
    planner = _build_planner(args.mode, settings)
    try:
        ir = planner.propose(
            facts,
            None,
            str(edl.get("goal") or ""),
            project_id="eval",
            revision=1,
            candidates={"candidates": edl.get("candidates") or []},
            chapters={"chapters": edl.get("chapters") or []},
            perception={
                "quality": {"marks": edl.get("quality_marks") or []},
            },
        )
    finally:
        planner.close()

    metrics = evaluate_selection(
        ir_intervals(ir),
        edl.get("kept_intervals") or [],
        source_duration=facts.duration.to_float(),
    )
    payload = {
        "edl": edl.get("name") or Path(args.edl).stem,
        "mode": args.mode,
        "goal": edl.get("goal"),
        "source_hash": digest,
        "metrics": metrics,
        "segments": [
            {
                "start": op.range.start.to_float(),
                "end": op.range.end.to_float(),
                "tag": op.tag,
                "rationale": op.rationale,
            }
            for op in ir.sequence
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)

    if args.min_f1 is not None and metrics["f1"] < args.min_f1:
        print(
            f"F1 {metrics['f1']:.4f} < 下限 {args.min_f1:.4f}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
