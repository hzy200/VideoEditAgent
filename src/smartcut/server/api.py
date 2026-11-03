"""REST 接口。P0 用轮询获取任务进度；SSE 延后。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.effects import Effect
from smartcut.contracts.patch import EditPatch
from smartcut.contracts.templates import EditTemplate
from smartcut.contracts.transcode import TranscodeRequest
from smartcut.errors import ResourceNotFound
from smartcut.server.services import Services


class CreateProjectRequest(BaseModel):
    name: str = Field(default="未命名项目", min_length=1, max_length=200)


class IngestRequest(BaseModel):
    src: str = Field(min_length=1, description="本地素材路径（会复制进 workspace）")


class RenameProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class AudioImportRequest(BaseModel):
    src: str = Field(min_length=1, description="本地音频路径（会复制进 workspace）")
    name: str | None = Field(default=None, max_length=200)


class SeparateRequest(BaseModel):
    model: str | None = Field(default=None, max_length=100)
    stems: int = Field(default=2, description="2 或 4")


class OverlayImportRequest(BaseModel):
    src: str = Field(min_length=1, description="本地图片路径（会复制进 workspace）")
    name: str | None = Field(default=None, max_length=200)


class PlanRequest(BaseModel):
    goal: str = Field(min_length=1)


class RevisionRequest(BaseModel):
    ir: EditIR


class PatchRequest(BaseModel):
    patch: EditPatch


class RenderRequest(BaseModel):
    revision: int = Field(ge=0)
    dry_run: bool = False


class OrchestrateRequest(BaseModel):
    goal: str = Field(min_length=1)
    allow_render: bool = True
    manual_ir: EditIR | None = None
    pinned_effects: list[Effect] | None = None
    phase: Literal["outline", "fast_outline", "audio_first", "plan"] = "plan"


class OutlineApproveRequest(BaseModel):
    chapters: list[dict[str, Any]] = Field(min_length=1)
    notes: str = ""


class RenderReviewRequest(BaseModel):
    goal: str = ""
    keyframe_count: int = 6


class ApplyTemplateRequest(BaseModel):
    template_id: str
    goal: str = ""


class BatchRequest(BaseModel):
    project_ids: list[str] = Field(min_length=1)
    template_id: str | None = None
    goal: str = ""
    allow_render: bool = False
    phase: Literal["outline", "fast_outline", "audio_first", "plan"] = "plan"


def get_services(request: Request) -> Services:
    return request.app.state.services


router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.post("/projects")
def create_project(
    payload: CreateProjectRequest,
    services: Services = Depends(get_services),
) -> dict:
    return services.create_project(payload.name)


@router.get("/projects/{project_id}")
def get_project(
    project_id: str,
    services: Services = Depends(get_services),
) -> dict:
    return services.get_project(project_id)


@router.patch("/projects/{project_id}")
def rename_project(
    project_id: str,
    payload: RenameProjectRequest,
    services: Services = Depends(get_services),
) -> dict:
    return services.rename_project(project_id, payload.name)


@router.delete("/projects/{project_id}")
def delete_project(
    project_id: str,
    services: Services = Depends(get_services),
) -> dict:
    return services.delete_project(project_id)


@router.post("/projects/{project_id}/ingest")
def ingest(
    project_id: str,
    payload: IngestRequest,
    services: Services = Depends(get_services),
) -> dict:
    return services.ingest(project_id, payload.src)


@router.get("/projects/{project_id}/audio-assets")
def list_audio_assets(
    project_id: str,
    services: Services = Depends(get_services),
) -> dict:
    return {"audio_assets": services.list_audio_assets(project_id)}


@router.post("/projects/{project_id}/audio-assets")
def import_audio_asset(
    project_id: str,
    payload: AudioImportRequest,
    services: Services = Depends(get_services),
) -> dict:
    return services.import_audio_asset(project_id, payload.src, payload.name)


@router.post("/projects/{project_id}/separate", status_code=202)
def separate_audio(
    project_id: str,
    payload: SeparateRequest,
    services: Services = Depends(get_services),
) -> dict:
    job_id = services.jobs.submit(
        project_id,
        "separate",
        lambda reporter: services.separate_audio(
            project_id,
            reporter,
            model=payload.model,
            stems=payload.stems,
        ),
    )
    return {"job_id": job_id, "project_id": project_id}


@router.post("/projects/{project_id}/upload")
def upload(
    project_id: str,
    file: UploadFile = File(...),
    services: Services = Depends(get_services),
) -> dict:
    """上传文件：直接写入项目工作区副本，不触碰任何源文件。"""
    return services.ingest_upload(project_id, file.filename or "upload.mp4", file.file)


@router.post("/projects/{project_id}/transcribe", status_code=202)
def transcribe(
    project_id: str,
    services: Services = Depends(get_services),
) -> dict:
    project = services.get_project(project_id)
    if project["asset"] is None:
        raise ResourceNotFound("请先导入素材")
    job_id = services.jobs.submit(
        project_id,
        "transcribe",
        lambda reporter: services.transcribe(project_id, reporter),
    )
    return {"job_id": job_id, "status": "queued"}


@router.post("/projects/{project_id}/plan")
def plan(
    project_id: str,
    payload: PlanRequest,
    services: Services = Depends(get_services),
) -> dict:
    return services.plan(project_id, payload.goal)


@router.get("/projects/{project_id}/candidates")
def get_candidates(
    project_id: str,
    services: Services = Depends(get_services),
) -> dict:
    candidates = services.get_candidates(project_id)
    return candidates or {"src": "", "candidates": []}


@router.get("/projects/{project_id}/candidates/search")
def search_candidates(
    project_id: str,
    q: str = "",
    limit: int = 60,
    reference: str | None = None,
    distance: int = 10,
    services: Services = Depends(get_services),
) -> dict:
    return services.retrieve_candidates(
        project_id,
        q,
        limit=max(1, min(limit, 200)),
        reference_candidate_id=reference,
        max_distance=distance,
    )


@router.get("/projects/{project_id}/revisions")
def list_revisions(
    project_id: str,
    services: Services = Depends(get_services),
) -> dict:
    return {"revisions": services.list_revisions(project_id)}


@router.post("/projects/{project_id}/revisions")
def create_revision(
    project_id: str,
    payload: RevisionRequest,
    services: Services = Depends(get_services),
) -> dict:
    return services.submit_revision(project_id, payload.ir, origin="user")


@router.post("/projects/{project_id}/revisions/{revision_no}/patch")
def apply_revision_patch(
    project_id: str,
    revision_no: int,
    payload: PatchRequest,
    services: Services = Depends(get_services),
) -> dict:
    return services.apply_patch(project_id, revision_no, payload.patch)


@router.get("/projects/{project_id}/revisions/{revision_no}")
def get_revision(
    project_id: str,
    revision_no: int,
    services: Services = Depends(get_services),
) -> dict:
    ir = services.get_revision(project_id, revision_no)
    return {"revision": revision_no, "ir": ir.dump_ir()}


@router.get("/projects/{project_id}/revisions/{revision_no}/diff/{base_revision}")
def revision_diff(
    project_id: str,
    revision_no: int,
    base_revision: int,
    services: Services = Depends(get_services),
) -> dict:
    return services.revision_diff(project_id, revision_no, base_revision)


@router.post("/projects/{project_id}/render")
def render(
    project_id: str,
    payload: RenderRequest,
    services: Services = Depends(get_services),
) -> dict:
    if payload.dry_run:
        plan = services.dry_run(project_id, payload.revision)
        return {"dry_run": True, **plan}
    services.get_revision(project_id, payload.revision)
    job_id = services.jobs.submit(
        project_id,
        "render",
        lambda reporter: services.render(project_id, payload.revision, reporter),
    )
    return {"job_id": job_id, "status": "queued"}


@router.get("/projects/{project_id}/overlay-assets")
def list_overlay_assets(
    project_id: str,
    services: Services = Depends(get_services),
) -> dict:
    return {"items": services.list_overlay_assets(project_id)}


@router.post("/projects/{project_id}/overlay-assets", status_code=201)
def import_overlay_asset(
    project_id: str,
    payload: OverlayImportRequest,
    services: Services = Depends(get_services),
) -> dict:
    return services.import_overlay_asset(
        project_id, payload.src, payload.name
    )


@router.delete("/projects/{project_id}/overlay-assets/{asset_id}")
def delete_overlay_asset(
    project_id: str,
    asset_id: str,
    services: Services = Depends(get_services),
) -> dict:
    return services.delete_overlay_asset(project_id, asset_id)


@router.post("/projects/{project_id}/transcode", status_code=202)
def transcode(
    project_id: str,
    payload: TranscodeRequest,
    services: Services = Depends(get_services),
) -> dict:
    """独立转码/封装：不进入 EditIR，也不改变项目时间线。"""
    services.get_project(project_id)
    services.get_media_facts(project_id)
    job_id = services.jobs.submit(
        project_id,
        "transcode",
        lambda reporter: services.transcode(project_id, payload, reporter),
    )
    return {"job_id": job_id, "status": "queued"}


@router.post("/projects/{project_id}/orchestrate", status_code=202)
def orchestrate(
    project_id: str,
    payload: OrchestrateRequest,
    services: Services = Depends(get_services),
) -> dict:
    """提交 Orchestrator 运行：动态任务图 + 并行子智能体 + 最终决策。"""
    run = services.create_orchestration_run(
        project_id,
        payload.goal,
        allow_render=payload.allow_render,
        manual_ir=payload.manual_ir,
        pinned_effects=payload.pinned_effects,
        phase=payload.phase,
    )
    run_id = run["id"]
    job_id = services.jobs.submit(
        project_id,
        "orchestrate",
        lambda reporter: services.orchestrate_run(run_id, reporter),
        on_cancel=lambda: services.cancel_orchestration_run(run_id),
    )
    return {"run_id": run_id, "job_id": job_id, "status": "queued"}


@router.get("/projects/{project_id}/runs")
def list_runs(
    project_id: str,
    services: Services = Depends(get_services),
) -> dict:
    return {"runs": services.list_orchestration_run_summaries(project_id)}


@router.get("/projects/{project_id}/runs/resumable")
def list_resumable_runs(
    project_id: str,
    services: Services = Depends(get_services),
) -> dict:
    services.get_project(project_id)
    return {"runs": services.list_resumable_runs(project_id)}


@router.get("/projects/{project_id}/runs/{run_id}")
def get_project_run(
    project_id: str,
    run_id: str,
    services: Services = Depends(get_services),
) -> dict:
    run = services.get_orchestration_run(run_id)
    if run["project_id"] != project_id:
        raise ResourceNotFound(f"运行不属于该项目: {run_id}")
    return run


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    services: Services = Depends(get_services),
) -> dict:
    return services.get_orchestration_run(run_id)


@router.get("/jobs")
def list_jobs(
    project_id: str | None = None,
    limit: int = 200,
    services: Services = Depends(get_services),
) -> dict:
    return {
        "jobs": services.jobs.list_jobs(
            project_id,
            limit=max(1, min(limit, 1000)),
        )
    }


@router.get("/jobs/{job_id}/events")
def job_events(
    job_id: str,
    services: Services = Depends(get_services),
) -> StreamingResponse:
    async def stream():
        last: str | None = None
        while True:
            job = services.jobs.get(job_id)
            if job is None:
                yield "event: error\ndata: {\"error\":\"job not found\"}\n\n"
                break
            payload = json.dumps(job, ensure_ascii=False, default=str)
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/runs/{run_id}/events")
def run_events(
    run_id: str,
    services: Services = Depends(get_services),
) -> StreamingResponse:
    terminal = {"succeeded", "failed", "partial", "cancelled", "awaiting_approval"}

    async def stream():
        last: str | None = None
        while True:
            run = services.get_orchestration_run(run_id)
            payload = json.dumps(run, ensure_ascii=False, default=str)
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
            if run["status"] in terminal:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/diagnostics")
def diagnostics(
    limit: int = 300,
    services: Services = Depends(get_services),
) -> dict:
    return services.diagnostics(limit=max(1, min(limit, 2000)))


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    services: Services = Depends(get_services),
) -> dict:
    job = services.jobs.get(job_id)
    if job is None:
        raise ResourceNotFound(f"任务不存在: {job_id}")
    return job


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    services: Services = Depends(get_services),
) -> dict:
    if not services.jobs.cancel(job_id):
        raise ResourceNotFound(f"任务不存在或已结束: {job_id}")
    return {"job_id": job_id, "status": "cancelling"}


@router.post("/runs/{run_id}/resume", status_code=202)
def resume_run(
    run_id: str,
    services: Services = Depends(get_services),
) -> dict:
    return services.resume_orchestration_run(run_id)


@router.get("/runs/{run_id}/outline")
def get_outline(
    run_id: str,
    services: Services = Depends(get_services),
) -> dict:
    return services.get_outline(run_id)


@router.post("/runs/{run_id}/approve-outline", status_code=202)
def approve_outline(
    run_id: str,
    payload: OutlineApproveRequest,
    services: Services = Depends(get_services),
) -> dict:
    return services.approve_outline(
        run_id,
        payload.chapters,
        notes=payload.notes,
    )


@router.get("/templates")
def list_templates(services: Services = Depends(get_services)) -> dict:
    return {"templates": services.list_templates()}


@router.post("/templates")
def create_template(
    payload: EditTemplate,
    services: Services = Depends(get_services),
) -> dict:
    return services.create_template(payload)


@router.delete("/templates/{template_id}")
def delete_template(
    template_id: str,
    services: Services = Depends(get_services),
) -> dict:
    return services.delete_template(template_id)


@router.post("/projects/{project_id}/apply-template", status_code=202)
def apply_template(
    project_id: str,
    payload: ApplyTemplateRequest,
    services: Services = Depends(get_services),
) -> dict:
    return services.apply_template(
        project_id,
        payload.template_id,
        extra_goal=payload.goal,
    )


@router.post("/batch/orchestrate", status_code=202)
def batch_orchestrate(
    payload: BatchRequest,
    services: Services = Depends(get_services),
) -> dict:
    return services.start_batch(
        payload.project_ids,
        template_id=payload.template_id,
        goal=payload.goal,
        allow_render=payload.allow_render,
        phase=payload.phase,
    )


@router.post("/projects/{project_id}/revisions/{revision_no}/review-render")
def review_render(
    project_id: str,
    revision_no: int,
    payload: RenderReviewRequest,
    services: Services = Depends(get_services),
) -> dict:
    return services.review_render(
        project_id,
        revision_no,
        goal=payload.goal,
        keyframe_count=payload.keyframe_count,
    )


@router.get("/projects/{project_id}/preference")
def get_edit_preference(
    project_id: str,
    services: Services = Depends(get_services),
) -> dict:
    preference = services.get_edit_preference(project_id)
    return {"available": preference is not None, "preference": preference}


@router.get("/projects/{project_id}/manifest")
def get_video_manifest(
    project_id: str,
    services: Services = Depends(get_services),
) -> dict:
    services.get_project(project_id)
    return services.build_video_manifest(project_id)


@router.get("/projects/{project_id}/artifacts")
def list_artifacts(
    project_id: str,
    services: Services = Depends(get_services),
) -> dict:
    services.get_project(project_id)
    return {"artifacts": services.artifacts(project_id)}
