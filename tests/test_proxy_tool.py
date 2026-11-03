from __future__ import annotations

import hashlib

from smartcut.contracts.tools import ToolCall
from smartcut.runner.execute import ToolCallExecutor
from smartcut.runner.paths import Workspace


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_proxy_generation_is_local_and_source_safe(
    executor: ToolCallExecutor,
    workspace: Workspace,
    ingested_source: str,
) -> None:
    source = workspace.resolve_input(ingested_source)
    before = _sha256(source)

    executor.execute(
        ToolCall(
            seq=0,
            tool="proxy",
            args={
                "src": ingested_source,
                "dst": "proxy/preview.mp4",
                "height": 720,
            },
        )
    )

    assert (workspace.proxy_dir / "preview.mp4").exists()
    facts = executor.probe("proxy/preview.mp4")
    assert facts.video is not None
    assert facts.video.height == 720
    assert facts.has_audio
    assert _sha256(source) == before
