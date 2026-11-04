"""桌面场景预设：复用 EditTemplate，不引入第二套配置格式。"""

from __future__ import annotations

from smartcut.contracts.templates import EditTemplate

SCENE_PRESETS: tuple[EditTemplate, ...] = (
    EditTemplate(
        id="preset_shorts",
        name="口播切片",
        goal="剪成 3 条 60 秒竖版切片，保留高信息密度片段，去掉口癖和长停顿",
        allow_render=True,
        phase="plan",
        aspect="9:16",
        burn_captions=True,
        loudness="-14 LUFS",
        remove_filler=True,
    ),
    EditTemplate(
        id="preset_interview",
        name="访谈精华",
        goal="按话题整理访谈精华，保留关键观点，去掉长停顿",
        allow_render=True,
        phase="audio_first",
        burn_captions=True,
        loudness="-14 LUFS",
    ),
    EditTemplate(
        id="preset_course",
        name="课程章节",
        goal="按章节粗剪课程，保留完整讲解，去掉无效片段",
        allow_render=True,
        phase="outline",
    ),
)
