"""屏幕文字服务：候选区检测、独立 worker OCR、按帧缓存与聚合。"""

from __future__ import annotations

import hashlib

from smartcut.contracts.screen_text import ScreenTextReport
from smartcut.errors import RunCancelledError
from smartcut.perception import frames as frames_mod
from smartcut.perception.screen_text import (
    aggregate_screen_text,
    build_screen_text_extractor,
    detect_text_regions,
)


class ScreenTextMixin:
    def build_screen_text(self, project_id: str, cancel_token=None) -> dict:
        asset = self._require_asset(project_id)
        if not self.settings.ocr_enabled:
            report = ScreenTextReport(
                src=asset.src,
                engine="disabled",
                version=self.settings.ocr_policy_version,
                available=False,
                warnings=["OCR 已关闭（SMARTCUT_OCR_ENABLED=false）"],
            )
            return self._screen_text_summary(report, cached=False)

        workspace = self.workspaces.get(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        frames_record = cache.latest(project_id, "frames", source_hash)
        if frames_record is None:
            report = ScreenTextReport(
                src=asset.src,
                engine="unavailable",
                version=self.settings.ocr_policy_version,
                available=False,
                warnings=["尚未抽帧，无法识别屏幕文字"],
            )
            return self._screen_text_summary(report, cached=False)
        frames = frames_mod.read_manifest(
            cache.resolve(frames_record["path"]) / "manifest.json"
        )
        if not frames:
            report = ScreenTextReport(
                src=asset.src,
                engine="unavailable",
                version=self.settings.ocr_policy_version,
                available=False,
                warnings=["抽帧清单为空"],
            )
            return self._screen_text_summary(report, cached=False)

        max_frames = max(1, int(self.settings.ocr_max_frames))
        if len(frames) > max_frames:
            step = len(frames) / max_frames
            selected = [frames[int(index * step)] for index in range(max_frames)]
        else:
            selected = frames

        extractor = getattr(self, "screen_text_extractor", None)
        if extractor is None:
            extractor = build_screen_text_extractor(self.settings)
            self.screen_text_extractor = extractor
        worker_version = str(
            getattr(extractor, "version", self.settings.ocr_policy_version)
        )
        report_key = cache.key(
            source_hash,
            frames_record["cache_key"],
            "screen_text_v1",
            worker_version,
            self.settings.ocr_policy_version,
            max_frames,
            self.settings.ocr_min_confidence,
            self.settings.ocr_min_stability,
            self.settings.ocr_region_max_per_frame,
        )
        cached = cache.get_json(project_id, "screen_text", report_key, source_hash)
        if cached is not None:
            return self._screen_text_summary(
                ScreenTextReport.model_validate(cached),
                cached=True,
            )

        results: list[dict | None] = [None] * len(selected)
        regions: list[list[tuple[float, float, float, float]]] = []
        missing: list[tuple[int, object, str]] = []
        for index, frame in enumerate(selected):
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            path = workspace.root / frame.path
            try:
                payload = path.read_bytes()
            except OSError:
                results[index] = {
                    "engine": worker_version,
                    "items": [],
                    "error": "frame unreadable",
                }
                regions.append([])
                continue
            frame_sha = hashlib.sha256(payload).hexdigest()
            frame_key = cache.key(
                source_hash,
                frame_sha,
                worker_version,
                self.settings.ocr_policy_version,
                "ocr_frame_v1",
            )
            cached_frame = cache.get_json(
                project_id, "ocr_frame", frame_key, source_hash
            )
            if cached_frame is not None:
                results[index] = cached_frame
            else:
                missing.append((index, path, frame_key))
            regions.append(
                detect_text_regions(
                    payload,
                    max_regions=self.settings.ocr_region_max_per_frame,
                )
            )

        if missing:
            try:
                raw = extractor.analyze(
                    [str(path) for _index, path, _key in missing],
                    cancel_check=(
                        cancel_token.raise_if_cancelled
                        if cancel_token is not None
                        else None
                    ),
                    timeout_seconds=self.settings.ocr_worker_timeout_seconds,
                )
            except RunCancelledError:
                raise
            except Exception as exc:
                raw = [
                    {
                        "engine": "unavailable",
                        "items": [],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    for _ in missing
                ]
            for (index, _path, frame_key), item in zip(missing, raw):
                results[index] = item
                cache.put_json(
                    project_id,
                    "ocr_frame",
                    frame_key,
                    item,
                    source_hash=source_hash,
                )

        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        items = aggregate_screen_text(
            selected,
            [item or {} for item in results],
            regions_by_index=regions,
            min_confidence=self.settings.ocr_min_confidence,
            min_stability=self.settings.ocr_min_stability,
        )
        engine = next(
            (
                str(item.get("engine"))
                for item in results
                if item and item.get("engine")
            ),
            worker_version,
        )
        warnings = self._unique(
            [
                str(item.get("error"))
                for item in results
                if item and item.get("error")
            ],
            5,
        )
        report = ScreenTextReport(
            src=asset.src,
            engine=engine,
            version=self.settings.ocr_policy_version,
            items=items,
            warnings=warnings,
            available=bool(items) or engine not in {"unavailable", "disabled"},
        )
        cache.put_json(
            project_id,
            "screen_text",
            report_key,
            report.model_dump(mode="json"),
            source_hash=source_hash,
            meta={
                "items": len(items),
                "frames": len(selected),
                "worker_version": worker_version,
            },
        )
        return self._screen_text_summary(report, cached=False)

    @staticmethod
    def _screen_text_summary(report: ScreenTextReport, *, cached: bool) -> dict:
        return {
            "src": report.src,
            "available": report.available,
            "item_count": len(report.items),
            "engine": report.engine,
            "version": report.version,
            "warnings": report.warnings,
            "report": report.model_dump(mode="json"),
            "cached": cached,
        }
