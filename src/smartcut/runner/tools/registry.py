"""工具白名单。Runner 只认这里的名字。"""

from __future__ import annotations

from smartcut.errors import SandboxError
from smartcut.runner.tools import (
    compose,
    concat,
    cut,
    extract_audio,
    fit,
    frames,
    loudnorm,
    loudnorm_measure,
    mix_audio,
    postprocess,
    probe,
    proxy,
    subtitles,
    transcode,
)
from smartcut.runner.tools.base import ToolDef
from smartcut.runner.verify import VerifyArgs

TOOLS: dict[str, ToolDef] = {
    "probe": ToolDef(name="probe", args_model=probe.ProbeArgs, build_argv=probe.build_argv),
    "cut": ToolDef(name="cut", args_model=cut.CutArgs, build_argv=cut.build_argv),
    "concat": ToolDef(
        name="concat",
        args_model=concat.ConcatArgs,
        build_argv=concat.build_argv,
        prepare=concat.prepare,
    ),
    "compose": ToolDef(
        name="compose",
        args_model=compose.ComposeArgs,
        build_argv=compose.build_argv,
        timeout_attr="render_timeout_seconds",
    ),
    "extract_audio": ToolDef(
        name="extract_audio",
        args_model=extract_audio.ExtractAudioArgs,
        build_argv=extract_audio.build_argv,
    ),
    "proxy": ToolDef(
        name="proxy",
        args_model=proxy.ProxyArgs,
        build_argv=proxy.build_argv,
    ),
    "extract_frames": ToolDef(
        name="extract_frames",
        args_model=frames.ExtractFramesArgs,
        build_argv=frames.build_argv,
        prepare=frames.prepare,
    ),
    "fit": ToolDef(
        name="fit",
        args_model=fit.FitArgs,
        build_argv=fit.build_argv,
    ),
    "subtitles": ToolDef(
        name="subtitles",
        args_model=subtitles.SubtitlesArgs,
        build_argv=subtitles.build_argv,
    ),
    "loudnorm": ToolDef(
        name="loudnorm",
        args_model=loudnorm.LoudnormArgs,
        build_argv=loudnorm.build_argv,
    ),
    "postprocess": ToolDef(
        name="postprocess",
        args_model=postprocess.PostProcessArgs,
        build_argv=postprocess.build_argv,
    ),
    "mix_audio": ToolDef(
        name="mix_audio",
        args_model=mix_audio.MixAudioArgs,
        build_argv=mix_audio.build_argv,
        timeout_attr="mix_timeout_seconds",
    ),
    "transcode": ToolDef(
        name="transcode",
        args_model=transcode.TranscodeArgs,
        build_argv=transcode.build_argv,
        prepare=transcode.prepare,
        timeout_attr="render_timeout_seconds",
    ),
    "loudnorm_measure": ToolDef(
        name="loudnorm_measure",
        args_model=loudnorm_measure.LoudnormMeasureArgs,
        build_argv=loudnorm_measure.build_argv,
        timeout_attr="render_timeout_seconds",
    ),
    "verify": ToolDef(name="verify", args_model=VerifyArgs),
}


def get_tool(name: str) -> ToolDef:
    try:
        return TOOLS[name]
    except KeyError as exc:
        raise SandboxError(f"未注册的工具: {name!r}") from exc
