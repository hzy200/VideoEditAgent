"""独立校验智能体：再次 probe 成片，与 IR/源事实比对。"""

from __future__ import annotations

from fractions import Fraction

from smartcut.contracts.time import frame_tolerance
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class VerifyAgent:
    name = "verify"
    description = "对成片做独立 ffprobe 校验：时长、音轨、分辨率"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        ctx.raise_if_cancelled()
        render = ctx.shared.render_result
        if render is None:
            raise RuntimeError("verify 缺少前置渲染结果")

        facts = ctx.shared.facts or ctx.toolkit.get_media_facts(ctx.project_id)
        ir = ctx.shared.ir
        output_path = render["output_path"]
        actual = ctx.toolkit.probe(ctx.project_id, output_path)

        warnings: list[str] = []
        checks: dict[str, bool] = {}

        if ir is not None:
            expected = ir.total_duration().as_fraction()
            fps = facts.fps_fraction
            tolerance = max(
                frame_tolerance(fps, 2) if fps else Fraction(1, 20),
                Fraction(1, 10),
            )
            diff = abs(actual.duration.as_fraction() - expected)
            checks["duration"] = diff <= tolerance
            if not checks["duration"]:
                warnings.append(
                    f"时长差异 {float(diff):.3f}s 超出容差 {float(tolerance):.3f}s"
                )
        else:
            diff = None

        checks["audio"] = actual.has_audio == facts.has_audio
        if not checks["audio"]:
            warnings.append("音轨存在性与源素材不一致")
        checks["resolution"] = (actual.width, actual.height) == (facts.width, facts.height)
        if not checks["resolution"]:
            warnings.append(
                f"分辨率不一致：{actual.width}x{actual.height} "
                f"!= {facts.width}x{facts.height}"
            )

        ok = all(checks.values())
        output = {
            "output_path": output_path,
            "duration_seconds": actual.duration.to_ffmpeg_seconds(),
            "diff_seconds": float(diff) if diff is not None else None,
            "has_audio": actual.has_audio,
            "width": actual.width,
            "height": actual.height,
            "checks": checks,
            "ok": ok,
        }
        ctx.set_verify(output)
        return AgentResult(
            summary="独立校验通过" if ok else "独立校验发现差异",
            output=output,
            warnings=warnings,
            artifacts=[output_path],
            confidence=1.0 if ok else 0.3,
        )
