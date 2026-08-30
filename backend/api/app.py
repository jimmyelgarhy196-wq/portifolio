"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api.deps import templates
from backend.core.config import FRONTEND_DIR, get_settings
from backend.core.database import init_database
from backend.core.logging_config import configure_logging, get_logger

logger = get_logger(__name__)

DESCRIPTION = """
Personal AI research and portfolio-management terminal for Egyptian Exchange (EGX)
listed equities.

**Paper trading and research only.** There is no broker integration in this system
and no live execution path. Every position is simulated.

Every stored datum carries its source, retrieval time and confidence. Missing data
is reported as unavailable rather than estimated, and every score is returned with
its full decomposition.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    init_database()

    if settings.allow_synthetic_data:
        logger.warning(
            "SYNTHETIC DATA IS ENABLED. Any figures shown may be fictional. "
            "Unset EGX_ALLOW_SYNTHETIC_DATA before relying on this terminal."
        )
    if not settings.ai_enabled:
        logger.info(
            "No ANTHROPIC_API_KEY set — research agents will use the deterministic "
            "narrative engine. The system is fully functional in this mode."
        )

    from backend.jobs.scheduler import shutdown_scheduler, start_scheduler

    start_scheduler()
    logger.info("EGX ALPHA ready on http://%s:%s", settings.host, settings.port)
    try:
        yield
    finally:
        shutdown_scheduler()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="EGX ALPHA",
        description=DESCRIPTION,
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    app.mount(
        "/static", StaticFiles(directory=str(FRONTEND_DIR / "static")), name="static"
    )

    from backend.api.routes_api import router as api_router
    from backend.api.routes_pages import router as pages_router

    app.include_router(api_router)
    app.include_router(pages_router)

    @app.exception_handler(404)
    async def not_found(request: Request, exc):  # noqa: ANN001
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        detail = getattr(exc, "detail", "The requested page does not exist.")
        return templates.TemplateResponse(
            request, "error.html", _error_context(settings, 404, detail), status_code=404
        )

    @app.exception_handler(500)
    async def server_error(request: Request, exc):  # noqa: ANN001
        logger.exception("Unhandled error on %s", request.url.path)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Internal server error"}, status_code=500)
        return templates.TemplateResponse(
            request, "error.html",
            _error_context(
                settings, 500,
                "An unexpected error occurred. Check the server logs for detail.",
            ),
            status_code=500,
        )

    return app


def _error_context(settings, code: int, message: str) -> dict:
    """Minimal context for error pages.

    Deliberately avoids touching the database: an error page that itself needs a
    working database cannot render the error that broke the database.
    """
    return {
        "settings": settings, "code": code, "message": message,
        "synthetic": False, "universe_status": None, "header": {},
        "new_alerts": 0, "ai_enabled": settings.ai_enabled, "active": "",
    }


app = create_app()
