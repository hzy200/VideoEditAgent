from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest

from smartcut.config import Settings
from smartcut.orchestrator.context import AgentContext
from smartcut.runner.paths import Workspace


def _ctx(tmp_path) -> AgentContext:
    return AgentContext(
        run_id="run_state",
        project_id="p_state",
        goal="state",
        toolkit=cast(Any, object()),
        workspace=Workspace(tmp_path / "ws"),
        settings=Settings(workspace_root=tmp_path / "ws"),
    )


def test_shared_snapshot_is_frozen_and_updates_atomically(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    old = ctx.shared
    ctx.set_frames({"count": 3})

    assert old.frames is None
    assert ctx.shared.frames == {"count": 3}
    with pytest.raises(FrozenInstanceError):
        ctx.shared.frames = {"count": 9}  # type: ignore[misc]

    ctx.add_warning("a")
    ctx.add_warning("a")
    ctx.add_warning("b")
    assert ctx.shared.warnings == ("a", "b")
    with pytest.raises(AttributeError):
        ctx.shared.warnings.append("c")  # type: ignore[attr-defined]

    ctx.set_quality({"nested": {"value": 1}})
    with pytest.raises(TypeError):
        ctx.shared.quality["x"] = 2  # type: ignore[index]
    with pytest.raises(TypeError):
        ctx.shared.quality["nested"]["value"] = 3  # type: ignore[index]


def test_shared_setters_do_not_expose_mutable_lists(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    ctx.set_tool_calls([{"tool": "cut"}])
    assert isinstance(ctx.shared.tool_calls, tuple)
    assert ctx.shared.revision is None
