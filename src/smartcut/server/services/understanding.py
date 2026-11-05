"""视频理解/问答服务：只读事实，不生成 EditIR、不改时间线。"""

from __future__ import annotations

import time
from typing import Any

from smartcut.contracts.media import MediaFacts
from smartcut.contracts.time import seconds_from_mapping


class UnderstandingMixin:
    def understand_video(
        self,
        project_id: str,
        question: str = "",
        reporter: Any | None = None,
    ) -> dict:
        """收集转写/章节/媒体事实，用文字回答用户问题；绝不创建 revision。"""
        self._require_project(project_id)
        started = time.perf_counter()
        cancel_token = getattr(reporter, "cancel_token", None)
        warnings: list[str] = []
        if reporter is not None:
            reporter.progress(5, "读取媒体事实")

        facts = self.get_media_facts(project_id)
        transcript = self.get_transcript(project_id)
        if (
            transcript is None
            and facts.has_audio
            and getattr(self.transcriber, "name", "") != "null"
        ):
            if reporter is not None:
                reporter.progress(15, "转写中")
            try:
                self.transcribe(project_id, reporter)
            except Exception as exc:
                warnings.append(
                    f"转写失败，仅使用视觉/章节信息: {type(exc).__name__}: {exc}"
                )
            transcript = self.get_transcript(project_id)
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()

        perception = self.get_perception(project_id)
        if facts.video is not None and not perception.get("frame_manifest"):
            if reporter is not None:
                reporter.progress(35, "抽取理解用关键帧")
            try:
                self.extract_frames(project_id, fps=1.0, cancel_token=cancel_token)
            except Exception as exc:
                warnings.append(
                    f"抽帧失败，章节可能不可用: {type(exc).__name__}: {exc}"
                )
            perception = self.get_perception(project_id)
        if facts.video is not None and perception.get("frame_manifest") and not perception.get("shots"):
            if reporter is not None:
                reporter.progress(50, "识别镜头结构")
            try:
                self.detect_scenes(project_id, cancel_token=cancel_token)
            except Exception as exc:
                warnings.append(
                    f"镜头识别失败: {type(exc).__name__}: {exc}"
                )
            perception = self.get_perception(project_id)
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()

        chapters = self.get_chapters(project_id)
        if chapters is None and perception.get("frame_manifest"):
            if reporter is not None:
                reporter.progress(65, "生成章节摘要")
            try:
                self.build_chapters(project_id, cancel_token=cancel_token)
            except Exception as exc:
                warnings.append(
                    f"章节生成失败: {type(exc).__name__}: {exc}"
                )
            chapters = self.get_chapters(project_id)

        chapter_items = list((chapters or {}).get("chapters") or [])
        transcript_items = list(getattr(transcript, "segments", []) or [])
        context = self._understanding_context(
            facts, chapter_items, transcript_items
        )
        if reporter is not None:
            reporter.progress(80, "生成回答")
        answer, llm_used = self._understanding_answer(
            project_id,
            question,
            context,
            cancel_token=cancel_token,
        )
        if reporter is not None:
            reporter.progress(100, "理解完成")
        return {
            "mode": "understanding",
            "question": question,
            "answer": answer,
            "llm_used": llm_used,
            "facts": {
                "duration_seconds": round(facts.duration.to_float(), 3),
                "width": facts.width,
                "height": facts.height,
                "fps": facts.fps,
                "has_audio": facts.has_audio,
                "vfr": facts.vfr,
            },
            "chapters": [
                {
                    "id": item.get("id"),
                    "start": item.get("start"),
                    "end": item.get("end"),
                    "title": item.get("title"),
                    "summary": item.get("summary"),
                }
                for item in chapter_items
            ],
            "transcript_segments": len(transcript_items),
            "warnings": warnings,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    @staticmethod
    def _understanding_context(
        facts: MediaFacts,
        chapters: list[dict],
        transcript_items: list[Any],
    ) -> str:
        lines = [
            "媒体事实：",
            f"- 时长 {facts.duration.to_float():.2f}s；"
            f"{facts.width or '-'}x{facts.height or '-'}；"
            f"fps={facts.fps or '-'}；"
            f"{'有' if facts.has_audio else '无'}音频；"
            f"vfr={facts.vfr}",
        ]
        if chapters:
            lines.append("章节结构：")
            for item in chapters:
                start = item.get("start") or {}
                end = item.get("end") or {}
                lines.append(
                    "- "
                    f"{seconds_from_mapping(start) or 0.0:.2f}-"
                    f"{seconds_from_mapping(end) or 0.0:.2f}s "
                    f"{item.get('title') or '未命名章节'}"
                    + (
                        f"：{str(item.get('summary')).strip()}"
                        if item.get("summary")
                        else ""
                    )
                )
        if transcript_items:
            lines.append("转写节选：")
            for segment in transcript_items[:60]:
                lines.append(
                    f"- {segment.start.to_float():.2f}-"
                    f"{segment.end.to_float():.2f}s "
                    f"{str(segment.text).strip()}"
                )
        else:
            lines.append("转写：当前没有可用文本。")
        return "\n".join(lines)

    def _understanding_answer(
        self,
        project_id: str,
        question: str,
        context: str,
        *,
        cancel_token: Any | None,
    ) -> tuple[str, bool]:
        llm = getattr(getattr(self, "planner", None), "llm", None)
        if llm is not None and getattr(llm, "available", False):
            try:
                answer = llm.chat_text(
                    [
                        {
                            "role": "system",
                            "content": (
                                "你是视频内容理解助手。只基于给定事实用中文回答，"
                                "不生成剪辑方案、不改时间线、不输出文件路径或命令。"
                                "信息不足时明确说明不知道。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"{context}\n\n用户问题："
                                f"{question or '请概括视频的大致内容。'}"
                            ),
                        },
                    ],
                    temperature=0.2,
                    cancel_token=cancel_token,
                    usage_scope=f"understand:{project_id}",
                )
                if str(answer).strip():
                    return str(answer).strip(), True
            except Exception as exc:
                self.logger.warning(
                    "understand_llm_failed",
                    project_id=project_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
        return self._rule_answer(question, context), False

    @staticmethod
    def _rule_answer(question: str, context: str) -> str:
        prefix = (
            f"问题：{question}\n\n" if question else "视频内容概览：\n\n"
        )
        return (
            prefix
            + context
            + "\n\n（当前未配置可用的文本 LLM，以上为基于转写/章节/媒体事实的规则总结。）"
        )


__all__ = ["UnderstandingMixin"]
