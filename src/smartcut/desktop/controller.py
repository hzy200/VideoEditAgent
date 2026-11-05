"""桌面控制器：GUI 与 Services/Orchestrator 之间的唯一桥梁。

- 短任务走 QThreadPool，结果通过 Qt Signal 回到主线程；
- 长任务复用 JobService，QTimer 轮询 job 表并转换为信号；
- 工作线程绝不直接触碰 QWidget。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal, Slot

from smartcut.cancel import CancellationToken
from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.effects import Effect
from smartcut.contracts.transcode import TranscodeRequest
from smartcut.desktop.workers import Task
from smartcut.logging import get_logger
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


class DesktopController(QObject):
    projects_changed = Signal(object)
    project_opened = Signal(object)
    asset_changed = Signal(object)
    proxy_ready = Signal(str)
    job_progress = Signal(object)
    job_finished = Signal(str, object)
    run_finished = Signal(object)
    revisions_changed = Signal(object)
    revision_loaded = Signal(object)
    runs_changed = Signal(object)
    artifacts_changed = Signal(object)
    perception_changed = Signal(object)
    outline_ready = Signal(object)
    outline_approved = Signal(object)
    resumable_runs_found = Signal(object)
    candidates_changed = Signal(object)
    diff_changed = Signal(object)
    waveform_changed = Signal(object)
    captions_changed = Signal(object)
    jobs_changed = Signal(object)
    diagnostics_changed = Signal(object)
    audio_assets_changed = Signal(object)
    audio_waveforms_changed = Signal(object)
    overlay_assets_changed = Signal(object)
    preview_mix_ready = Signal(object)
    effects_preview_ready = Signal(object)
    render_reviewed = Signal(object)
    preference_changed = Signal(object)
    evidence_changed = Signal(object)
    operation_done = Signal(str, object)
    error = Signal(str)

    def __init__(
        self,
        settings: Settings,
        services: Services,
        jobs: JobService,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.services = services
        self.jobs = jobs
        self.pool = QThreadPool.globalInstance()
        self.logger = get_logger("smartcut.desktop")
        self._current_project: str | None = None
        self._watched: dict[str, dict[str, Any]] = {}
        self._jobs_poll_inflight = False
        self._open_inflight: str | None = None
        self._waveform_inflight: set[str] = set()
        self._proxy_inflight: set[str] = set()
        self._effects_preview_token: CancellationToken | None = None

        self._poll = QTimer(self)
        self._poll.setInterval(250)
        self._poll.timeout.connect(self._poll_jobs)
        self._queue_poll = QTimer(self)
        self._queue_poll.setInterval(1000)
        self._queue_poll.timeout.connect(self.refresh_jobs)

    @property
    def current_project(self) -> str | None:
        return self._current_project

    def has_active_jobs(self) -> bool:
        return bool(self._watched)

    def shutdown(self) -> None:
        self._poll.stop()
        self._queue_poll.stop()
        self.pool.waitForDone(2000)

    # ── 异步任务 ──────────────────────────────────────────────────────
    def _run(self, op: str, fn) -> None:
        task = Task(op, fn)
        task.signals.finished.connect(self._on_task_done)
        task.signals.failed.connect(self._on_task_failed)
        self.pool.start(task)

    @Slot(str, object)
    def _on_task_done(self, op: str, result: object) -> None:
        if op != "jobs_poll":
            self.logger.info("desktop_task_done", op=op)
        try:
            self._dispatch(op, result)
        except Exception as exc:  # 保护 Qt 事件循环
            self.logger.exception("desktop_dispatch_failed", op=op, error=str(exc))
            self.error.emit(f"{op} 结果处理失败: {type(exc).__name__}: {exc}")
        if op != "jobs_poll":
            self.operation_done.emit(op, result)

    @Slot(str, str)
    def _on_task_failed(self, op: str, error: str) -> None:
        if op == "jobs_poll":
            self._jobs_poll_inflight = False
            self.logger.warning("desktop_jobs_poll_failed", error=error)
            return
        self.logger.error("desktop_task_failed", op=op, error=error)
        if op == "open_project":
            self._open_inflight = None
        elif op == "audio_waveforms":
            self._waveform_inflight.clear()
        elif op == "proxy":
            self._proxy_inflight.clear()
        self.error.emit(f"{op}: {error}")
        self.operation_done.emit(f"{op}_failed", {"error": error})

    def _dispatch(self, op: str, result: Any) -> None:
        current = self._current_project
        if op == "list_projects":
            self.projects_changed.emit(result)
        elif op == "create_project":
            self.refresh_projects()
            self.open_project(result["id"])
        elif op == "open_project":
            opened_id = result["project"]["id"]
            if self._open_inflight == opened_id:
                self._open_inflight = None
            if opened_id != current:
                return
            self.project_opened.emit(result["project"])
            self.revisions_changed.emit(result["revisions"])
            self.runs_changed.emit(result["runs"])
            self.artifacts_changed.emit(result["artifacts"])
            self.perception_changed.emit(result.get("perception") or {})
            # project_opened 已经填充音频素材并触发一次波形构建；
            # 不再重复发 audio_assets_changed，避免清空/重复任务。
            self.refresh_preference(result["project"]["id"])
            if result.get("outline"):
                self.outline_ready.emit(result["outline"])
            if result.get("resumable_runs"):
                self.resumable_runs_found.emit(result["resumable_runs"])
            proxy = result.get("proxy") or {}
            if proxy.get("proxy"):
                workspace = self.services.project_workspace(current)
                self.proxy_ready.emit(str(workspace.resolve(proxy["proxy"])))
            if result["project"].get("asset") and not proxy.get("proxy"):
                self.build_proxy(result["project"]["id"])
        elif op == "list_revisions":
            if result["project_id"] == current:
                self.revisions_changed.emit(result["items"])
        elif op == "list_runs":
            if result["project_id"] == current:
                self.runs_changed.emit(result["items"])
        elif op == "artifacts":
            if result["project_id"] == current:
                self.artifacts_changed.emit(result["items"])
        elif op == "ingest":
            if result.get("project_id") != current:
                return
            self.asset_changed.emit(result)
            self.open_project(result["project_id"])
        elif op == "import_audio":
            if result.get("project_id") == current:
                self.refresh_audio_assets()
        elif op == "audio_assets":
            if result.get("project_id") == current:
                self.audio_assets_changed.emit(result.get("items") or [])
        elif op == "overlay_assets":
            if result.get("project_id") == current:
                self.overlay_assets_changed.emit(result.get("items") or [])
        elif op == "audio_waveforms":
            self._waveform_inflight.discard(result.get("project_id"))
            if result.get("project_id") == current:
                self.audio_waveforms_changed.emit(result.get("data") or {})
        elif op == "preview_mix":
            if result.get("project_id") == current:
                self.preview_mix_ready.emit(result.get("data") or {})
        elif op == "effects_preview":
            if (
                result.get("project_id") == current
                and result.get("token") is self._effects_preview_token
            ):
                self.effects_preview_ready.emit(result.get("data") or {})
        elif op == "proxy":
            self._proxy_inflight.discard(result.get("project_id"))
            proxy = result.get("proxy")
            project_id = result.get("project_id")
            if proxy and project_id and project_id == current:
                workspace = self.services.project_workspace(project_id)
                self.proxy_ready.emit(str(workspace.resolve(proxy)))
        elif op == "revision_loaded":
            if result.get("project_id") == current:
                self.revision_loaded.emit(result)
        elif op in {"plan", "revision"}:
            if result.get("project_id") == current:
                self.refresh_revisions()
                self.refresh_preference()
        elif op == "preference":
            if result.get("project_id") == current:
                self.preference_changed.emit(result.get("preference"))
        elif op == "segment_reject":
            if int(result.get("recorded") or 0) > 0:
                self.refresh_preference()
        elif op == "evidence":
            if result.get("project_id") == current:
                self.evidence_changed.emit(result)
        elif op == "run_detail":
            if result.get("project_id") == current:
                self.run_finished.emit(result)
        elif op == "render_review":
            if result.get("project_id") == current:
                self.render_reviewed.emit(result.get("review") or {})
        elif op == "perception":
            if result.get("project_id") == current:
                self.perception_changed.emit(result.get("data") or {})
        elif op == "outline":
            if result.get("project_id") == current:
                self.outline_ready.emit(result.get("outline") or {})
        elif op == "outline_approve":
            if result.get("project_id") != current:
                return
            job_id = result.get("job_id")
            if job_id:
                self._watched[job_id] = {
                    "kind": "orchestrate",
                    "project_id": result["project_id"],
                    "run_id": result.get("run_id"),
                }
                self._poll.start()
                self._queue_poll.start()
                self.refresh_jobs()
            self.outline_approved.emit(result)
        elif op == "candidates":
            if result.get("project_id") == current:
                self.candidates_changed.emit(result.get("data") or {})
        elif op == "revision_diff":
            if result.get("project_id") == current:
                self.diff_changed.emit(result.get("diff") or {})
        elif op == "waveform":
            if result.get("project_id") == current:
                self.waveform_changed.emit(result.get("data") or {})
        elif op == "captions":
            if result.get("project_id") == current:
                self.captions_changed.emit(result.get("data") or {})
        elif op == "jobs":
            project_id = result.get("project_id")
            if project_id is None or project_id == current:
                self.jobs_changed.emit(result)
        elif op == "jobs_poll":
            self._apply_job_snapshot(result or [])
        elif op == "orchestrate_create":
            job_id = result.get("job_id")
            if job_id:
                self._track_job(
                    job_id,
                    "orchestrate",
                    result["project_id"],
                    result.get("run_id"),
                )
        elif op == "resume_run":
            job_id = result.get("job_id")
            if job_id:
                self._track_job(
                    job_id,
                    "orchestrate",
                    result["project_id"],
                    result.get("run_id"),
                )
        elif op == "cancel_jobs":
            self.refresh_jobs()
        elif op == "job_submit":
            job_id = result.get("job_id")
            if job_id:
                self._track_job(
                    job_id,
                    result["kind"],
                    result["project_id"],
                    result.get("run_id"),
                )
        elif op == "diagnostics":
            self.diagnostics_changed.emit(result)
        elif op == "rename_project":
            self.refresh_projects()
            if result.get("id") == current:
                self.project_opened.emit(result)
        elif op == "delete_project":
            if result.get("deleted") == current:
                self._current_project = None
            self.refresh_projects()

    # ── 项目/素材 ─────────────────────────────────────────────────────
    def refresh_projects(self) -> None:
        self._run("list_projects", self.services.list_projects)

    def create_project(self, name: str) -> None:
        self._run("create_project", lambda: self.services.create_project(name))

    def open_project(self, project_id: str) -> None:
        if self._open_inflight == project_id:
            return
        self._open_inflight = project_id
        self._current_project = project_id

        def load_bundle() -> dict:
            # 独立查询并行执行；SQLite WAL + 只读事务允许并发读取。
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="bundle") as pool:
                f_project = pool.submit(self.services.get_project, project_id)
                f_revisions = pool.submit(self.services.list_revisions, project_id)
                f_runs = pool.submit(
                    self.services.list_orchestration_run_summaries,
                    project_id,
                )
                f_artifacts = pool.submit(self.services.artifacts, project_id)
                f_resumable = pool.submit(
                    self.services.list_resumable_runs,
                    project_id,
                )
                project = f_project.result()
                has_asset = bool(project.get("asset"))
                f_perception = (
                    pool.submit(self.services.get_perception, project_id)
                    if has_asset
                    else None
                )
                f_proxy = (
                    pool.submit(self.services.latest_proxy, project_id)
                    if has_asset
                    else None
                )
                f_manifest = (
                    pool.submit(self.services.build_video_manifest, project_id)
                    if has_asset
                    else None
                )
                runs = f_runs.result()
                pending = next(
                    (item for item in runs if item.get("status") == "awaiting_approval"),
                    None,
                )
                f_outline = (
                    pool.submit(self.services.get_outline, pending["id"])
                    if pending is not None
                    else None
                )
                perception = f_perception.result() if f_perception else {}
                if f_manifest is not None:
                    manifest = f_manifest.result()
                    perception = {
                        **(perception or {}),
                        "video_manifest": manifest.get("manifest") or {},
                        "manifest_meta": {
                            key: value
                            for key, value in manifest.items()
                            if key != "manifest"
                        },
                    }
                return {
                    "project": project,
                    "revisions": f_revisions.result(),
                    "runs": runs,
                    "artifacts": f_artifacts.result(),
                    "perception": perception,
                    "proxy": f_proxy.result() if f_proxy else None,
                    "outline": f_outline.result() if f_outline else None,
                    "resumable_runs": f_resumable.result(),
                }

        self._run("open_project", load_bundle)

    def refresh_revisions(self, project_id: str | None = None) -> None:
        pid = project_id or self._current_project
        if pid:
            self._run(
                "list_revisions",
                lambda: {"project_id": pid, "items": self.services.list_revisions(pid)},
            )

    def refresh_runs(self, project_id: str | None = None) -> None:
        pid = project_id or self._current_project
        if pid:
            self._run(
                "list_runs",
                lambda: {
                    "project_id": pid,
                    "items": self.services.list_orchestration_run_summaries(pid),
                },
            )

    def refresh_artifacts(self, project_id: str | None = None) -> None:
        pid = project_id or self._current_project
        if pid:
            self._run(
                "artifacts",
                lambda: {"project_id": pid, "items": self.services.artifacts(pid)},
            )

    def import_media(self, path: str | Path) -> None:
        pid = self._require_project()
        if not pid:
            return

        def ingest() -> dict:
            result = self.services.ingest(pid, str(path))
            result["project_id"] = pid
            return result

        self._run("ingest", ingest)

    def rename_project(self, project_id: str, name: str) -> None:
        self._run(
            "rename_project",
            lambda: self.services.rename_project(project_id, name),
        )

    def delete_project(self, project_id: str) -> None:
        self._run(
            "delete_project",
            lambda: self.services.delete_project(project_id),
        )

    def replace_media(self, path: str | Path) -> None:
        self.import_media(path)

    @property
    def separation_available(self) -> bool:
        return self.services.separation_available

    def import_audio(self, path: str | Path) -> None:
        pid = self._require_project()
        if not pid:
            return
        self._run(
            "import_audio",
            lambda: {
                "project_id": pid,
                "asset": self.services.import_audio_asset(pid, str(path)),
            },
        )

    def import_overlay_asset(self, path: str | Path) -> None:
        pid = self._require_project()
        if not pid:
            return

        def call() -> dict:
            self.services.import_overlay_asset(pid, str(path))
            return {
                "project_id": pid,
                "items": self.services.list_overlay_assets(pid),
            }

        self._run("overlay_assets", call)

    def refresh_audio_assets(self) -> None:
        pid = self._current_project
        if not pid:
            return
        self._run(
            "audio_assets",
            lambda: {
                "project_id": pid,
                "items": self.services.list_audio_assets(pid),
            },
        )

    def load_audio_waveforms(self) -> None:
        pid = self._current_project
        if not pid or pid in self._waveform_inflight:
            return
        self._waveform_inflight.add(pid)
        self._run(
            "audio_waveforms",
            lambda: {
                "project_id": pid,
                "data": self.services.build_audio_waveforms(pid),
            },
        )

    def build_preview_mix(self, ir: EditIR) -> None:
        pid = self._current_project
        if not pid:
            return
        self._run(
            "preview_mix",
            lambda: {
                "project_id": pid,
                "data": self.services.build_preview_mix(pid, ir),
            },
        )

    def preview_effects(self, ir: EditIR) -> None:
        pid = self._current_project
        if not pid:
            return
        if self._effects_preview_token is not None:
            self._effects_preview_token.cancel()
        token = CancellationToken()
        self._effects_preview_token = token

        def call() -> dict:
            return {
                "project_id": pid,
                "token": token,
                "data": self.services.render_effects_preview(
                    pid, ir, cancel_token=token
                ),
            }

        self._run("effects_preview", call)

    def separate_audio(
        self,
        model: str | None = None,
        stems: int = 2,
    ) -> None:
        pid = self._require_project()
        if not pid:
            return
        self._submit_job(
            "separate",
            pid,
            lambda reporter: self.services.separate_audio(
                pid,
                reporter,
                model=model,
                stems=stems,
            ),
        )

    def refresh_jobs(self) -> None:
        pid = self._current_project
        if not pid:
            self.jobs_changed.emit({"jobs": [], "project_id": None})
            return
        self._run(
            "jobs",
            lambda: {
                "project_id": pid,
                "jobs": self.jobs.list_jobs(pid, limit=200),
            },
        )

    def load_diagnostics(self) -> None:
        self._run("diagnostics", lambda: self.services.diagnostics())

    def upload_media(self, path: str | Path) -> None:
        pid = self._require_project()
        if not pid:
            return

        def upload() -> dict:
            with open(path, "rb") as handle:
                result = self.services.ingest_upload(pid, Path(path).name, handle)
            result["project_id"] = pid
            return result

        self._run("ingest", upload)

    def build_proxy(self, project_id: str | None = None) -> None:
        pid = project_id or self._current_project
        if not pid or pid in self._proxy_inflight:
            return
        self._proxy_inflight.add(pid)

        def proxy() -> dict:
            result = self.services.create_proxy(pid)
            result["project_id"] = pid
            return result

        self._run("proxy", proxy)

    # ── 转写 / 规划 / 编排 / 渲染 ─────────────────────────────────────
    def transcribe(self) -> None:
        pid = self._require_project()
        if not pid:
            return
        self._submit_job(
            "transcribe",
            pid,
            lambda reporter: self.services.transcribe(pid, reporter),
        )

    def plan(self, goal: str) -> None:
        pid = self._require_project()
        if not pid:
            return

        def call() -> dict:
            result = self.services.plan(pid, goal)
            result["project_id"] = pid
            return result

        self._run("plan", call)

    def orchestrate(
        self,
        goal: str,
        allow_render: bool,
        manual_ir: EditIR | None = None,
        pinned_effects: list[Effect] | None = None,
        phase: str = "plan",
    ) -> None:
        pid = self._require_project()
        if not pid:
            return
        pinned_count = len(pinned_effects or [])

        def create() -> dict:
            run = self.services.create_orchestration_run(
                pid,
                goal,
                allow_render=allow_render,
                manual_ir=manual_ir,
                pinned_effects=pinned_effects,
                phase=phase,
            )
            run_id = run["id"]
            job_id = self.jobs.submit(
                pid,
                "orchestrate",
                lambda reporter: self.services.orchestrate_run(run_id, reporter),
                on_cancel=lambda: self.services.cancel_orchestration_run(run_id),
            )
            return {
                "project_id": pid,
                "run_id": run_id,
                "job_id": job_id,
                "phase": phase,
                "pinned_count": pinned_count,
            }

        self._run("orchestrate_create", create)

    def submit_revision(self, ir: EditIR) -> None:
        pid = self._require_project()
        if not pid:
            return

        def call() -> dict:
            result = self.services.submit_revision(pid, ir, origin="user")
            result["project_id"] = pid
            return result

        self._run("revision", call)

    def record_segment_reject(self, segments: list[dict]) -> None:
        pid = self._require_project()
        if not pid or not segments:
            return

        def call() -> dict:
            result = self.services.record_segment_reject(pid, segments)
            result["project_id"] = pid
            return result

        self._run("segment_reject", call)

    def build_evidence(self, kind: str = "contact_sheet") -> None:
        pid = self._require_project()
        if not pid:
            return

        def call() -> dict:
            result = self.services.build_visual_evidence(pid, kind=kind)
            result["project_id"] = pid
            return result

        self._run("evidence", call)

    def refresh_preference(self, project_id: str | None = None) -> None:
        pid = project_id or self._require_project()
        if not pid:
            return

        def call() -> dict:
            # 偏好是可选增强：项目尚未 ingest 时返回 None，不产生错误提示。
            try:
                preference = self.services.get_edit_preference(pid)
            except Exception:
                preference = None
            return {"project_id": pid, "preference": preference}

        self._run("preference", call)

    def set_preference_scope(self, scope: str) -> None:
        value = "project" if scope == "project" else "source"
        self.settings.preference_scope = value
        self.services.settings.preference_scope = value
        self.refresh_preference()

    def load_revision(self, revision_no: int) -> None:
        pid = self._require_project()
        if not pid:
            return

        def load() -> dict:
            ir = self.services.get_revision(pid, revision_no)
            return {"project_id": pid, "revision": revision_no, "ir": ir.dump_ir()}

        self._run("revision_loaded", load)

    def dry_run(self, revision_no: int) -> None:
        pid = self._require_project()
        if not pid:
            return
        self._run(
            "dry_run",
            lambda: {"project_id": pid, "plan": self.services.dry_run(pid, revision_no)},
        )

    def render(self, revision_no: int) -> None:
        pid = self._require_project()
        if not pid:
            return
        self._submit_job(
            "render",
            pid,
            lambda reporter: self.services.render(pid, revision_no, reporter),
        )

    def transcode(self, request: TranscodeRequest) -> None:
        pid = self._require_project()
        if not pid:
            return
        self._submit_job(
            "transcode",
            pid,
            lambda reporter: self.services.transcode(pid, request, reporter),
        )

    def review_render(self, project_id: str, revision_no: int) -> None:
        def load() -> dict:
            return {
                "project_id": project_id,
                "review": self.services.review_render(project_id, revision_no),
            }

        self._run("render_review", load)

    def load_run(self, run_id: str) -> None:
        self._run("run_detail", lambda: self.services.get_orchestration_run(run_id))

    def load_perception(self, project_id: str | None = None) -> None:
        pid = project_id or self._current_project
        if pid:

            def load() -> dict:
                data = self.services.get_perception(pid)
                manifest = self.services.build_video_manifest(pid)
                return {
                    "project_id": pid,
                    "data": {
                        **(data or {}),
                        "video_manifest": manifest.get("manifest") or {},
                        "manifest_meta": {
                            key: value
                            for key, value in manifest.items()
                            if key != "manifest"
                        },
                    },
                }

            self._run("perception", load)

    def load_outline(self, run_id: str) -> None:
        def load() -> dict:
            run = self.services.get_orchestration_run(run_id)
            return {
                "project_id": run["project_id"],
                "outline": self.services.get_outline(run_id),
            }

        self._run("outline", load)

    def approve_outline(self, run_id: str, chapters: list[dict]) -> None:
        def approve() -> dict:
            result = self.services.approve_outline(run_id, chapters)
            result["project_id"] = self.services.get_orchestration_run(
                result["run_id"]
            )["project_id"]
            return result

        self._run("outline_approve", approve)

    def search_candidates(self, query: str) -> None:
        pid = self._require_project()
        if not pid:
            return

        def search() -> dict:
            return {
                "project_id": pid,
                "query": query,
                "data": self.services.retrieve_candidates(pid, query, limit=100),
            }

        self._run("candidates", search)

    def find_similar_candidates(self, candidate_id: str) -> None:
        """按候选代表帧的 dHash 找视觉相似片段。"""
        pid = self._require_project()
        if not pid or not candidate_id:
            return

        def search() -> dict:
            return {
                "project_id": pid,
                "query": f"similar:{candidate_id}",
                "data": self.services.retrieve_candidates(
                    pid,
                    "",
                    reference_candidate_id=candidate_id,
                    limit=100,
                ),
            }

        self._run("candidates", search)

    def diff_revisions(self, revision_no: int, base_revision_no: int) -> None:
        pid = self._require_project()
        if not pid:
            return

        def compare() -> dict:
            return {
                "project_id": pid,
                "diff": self.services.revision_diff(
                    pid,
                    revision_no,
                    base_revision_no,
                ),
            }

        self._run("revision_diff", compare)

    def load_waveform(self) -> None:
        pid = self._require_project()
        if not pid:
            return

        def load() -> dict:
            return {"project_id": pid, "data": self.services.build_waveform(pid)}

        self._run("waveform", load)

    def load_captions(self, revision_no: int) -> None:
        pid = self._require_project()
        if not pid:
            return

        def load() -> dict:
            return {
                "project_id": pid,
                "data": self.services.caption_preview(pid, revision_no),
            }

        self._run("captions", load)

    def cancel_active_jobs(self) -> int:
        return self.cancel_jobs(list(self._watched))

    def cancel_jobs(self, job_ids: list[str]) -> int:
        pending = [job_id for job_id in job_ids if job_id]
        if not pending:
            return 0
        self._run(
            "cancel_jobs",
            lambda: {
                "count": sum(1 for job_id in pending if self.jobs.cancel(job_id)),
            },
        )
        return len(pending)

    def resume_run(self, run_id: str) -> None:
        self._run(
            "resume_run",
            lambda: self.services.resume_orchestration_run(run_id),
        )

    # ── Job 轮询 ──────────────────────────────────────────────────────
    def _track_job(
        self,
        job_id: str,
        kind: str,
        project_id: str,
        run_id: str | None = None,
    ) -> None:
        self._watched[job_id] = {
            "kind": kind,
            "project_id": project_id,
            "run_id": run_id,
        }
        self._poll.start()
        self._queue_poll.start()
        self.refresh_jobs()

    def _submit_job(
        self,
        kind: str,
        project_id: str,
        handler,
        run_id: str | None = None,
        on_cancel=None,
    ) -> None:
        def submit() -> dict:
            job_id = self.jobs.submit(
                project_id,
                kind,
                handler,
                on_cancel=on_cancel,
            )
            return {
                "job_id": job_id,
                "kind": kind,
                "project_id": project_id,
                "run_id": run_id,
            }

        self._run("job_submit", submit)

    def _poll_jobs(self) -> None:
        """UI 线程只提交轮询任务；jobs.get 的 SQLite 读取放到工作线程。"""
        if self._jobs_poll_inflight or not self._watched:
            return
        self._jobs_poll_inflight = True
        watched = {job_id: dict(meta) for job_id, meta in self._watched.items()}

        def poll() -> list[tuple[str, dict[str, Any], dict | None]]:
            return [
                (job_id, meta, self.jobs.get(job_id))
                for job_id, meta in watched.items()
            ]

        self._run("jobs_poll", poll)

    def _apply_job_snapshot(
        self,
        items: list[tuple[str, dict[str, Any], dict | None]],
    ) -> None:
        try:
            for job_id, meta, job in items:
                if job_id not in self._watched or job is None:
                    continue
                is_current = meta["project_id"] == self._current_project
                if is_current:
                    self.job_progress.emit(job)
                if job["status"] not in {"succeeded", "failed", "cancelled"}:
                    continue
                self._watched.pop(job_id, None)
                if is_current:
                    self.job_finished.emit(job_id, job)
                kind = meta["kind"]
                project_id = meta["project_id"]
                if kind == "orchestrate":
                    self.refresh_runs(project_id)
                    if job["status"] == "succeeded" and job.get("result"):
                        if is_current:
                            self.run_finished.emit(job["result"])
                        self.refresh_revisions(project_id)
                        if is_current:
                            self.load_perception(project_id)
                elif kind == "render":
                    self.refresh_artifacts(project_id)
                elif kind == "transcode":
                    self.refresh_artifacts(project_id)
                    if job["status"] == "succeeded" and is_current:
                        self.operation_done.emit(
                            "transcode", job.get("result") or {}
                        )
                elif kind == "separate":
                    if job["status"] == "succeeded":
                        self.refresh_audio_assets()
                        if is_current:
                            self.operation_done.emit(
                                "separation", job.get("result") or {}
                            )
                elif kind == "transcribe":
                    if is_current:
                        self.operation_done.emit(
                            "transcribe", job.get("result") or {}
                        )
        finally:
            self._jobs_poll_inflight = False
            if not self._watched:
                self._poll.stop()
                self.refresh_jobs()
                self._queue_poll.stop()

    # ── 工具 ──────────────────────────────────────────────────────────
    def _require_project(self) -> str | None:
        if not self._current_project:
            self.error.emit("请先选择或创建项目")
            return None
        return self._current_project
