"""应用工厂与异常映射。

启动：``uvicorn smartcut.server.main:create_app --factory --port 8000``
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from smartcut.config import Settings, load_settings
from smartcut.errors import (
    IRValidationError,
    PlannerResponseError,
    PlannerUnavailable,
    ResourceNotFound,
    SandboxError,
    SmartCutError,
    ToolExecutionError,
    UnsupportedFeature,
    VerificationError,
)
from smartcut.logging import configure_logging, get_logger
from smartcut.perception.transcribe import Transcriber
from smartcut.planner.client import DeepSeekPlanner
from smartcut.server.api import router
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def create_app(
    settings: Settings | None = None,
    *,
    transcriber: Transcriber | None = None,
    planner: DeepSeekPlanner | None = None,
    view_renderer: Any | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    settings.ensure_dirs()
    configure_logging(settings.log_file)
    logger = get_logger("smartcut.api")

    db = Database(settings.db_path)
    db.init()
    jobs = JobService(
        db,
        settings.max_workers,
        logger,
        max_pending=settings.max_pending_jobs,
        tool_audit_retention_days=settings.tool_audit_retention_days,
    )
    services = Services(
        settings,
        db,
        jobs,
        logger=logger,
        transcriber=transcriber,
        planner=planner,
        view_renderer=view_renderer,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        # 先取消并限时等待任务，再关闭 client/DB；不能反向操作。
        jobs.shutdown(wait=True, timeout=15.0, cancel=True)
        services.close()
        db.dispose()

    app = FastAPI(title="SmartCut API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.services = services
    app.include_router(router)

    @app.exception_handler(ResourceNotFound)
    async def _not_found(_request: Request, exc: ResourceNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(IRValidationError)
    @app.exception_handler(UnsupportedFeature)
    async def _unprocessable(_request: Request, exc: SmartCutError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(SandboxError)
    async def _bad_path(_request: Request, exc: SandboxError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(PlannerUnavailable)
    async def _planner_unavailable(_request: Request, exc: PlannerUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(PlannerResponseError)
    async def _planner_failed(_request: Request, exc: PlannerResponseError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.exception_handler(ToolExecutionError)
    @app.exception_handler(VerificationError)
    async def _tool_failed(_request: Request, exc: SmartCutError) -> JSONResponse:
        logger.error("request_tool_failure", error=str(exc))
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _model_validation(_request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": json.loads(exc.json(include_url=False))},
        )

    @app.exception_handler(SmartCutError)
    async def _domain_error(_request: Request, exc: SmartCutError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
