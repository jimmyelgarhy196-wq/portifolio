"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.api.deps import templates
from backend.core.config import BRAND_COMPANY, BRAND_PRODUCT, FRONTEND_DIR, get_settings
from backend.core.database import init_database
from backend.core.logging_config import configure_logging, get_logger

logger = get_logger(__name__)

DESCRIPTION = """
**GMG Investment Intelligence** — subscription research and analytics for equities listed
on the Egyptian Exchange (EGX), by GMG AI Solutions.

**Information and research only.** GMG does not accept or hold client money, does not hold
securities, does not execute trades, does not manage portfolios for clients, and guarantees
no return. Nothing produced by this API is personal investment advice.

Every stored datum carries its source, retrieval time and confidence. Market data is
labelled with its true freshness — live, delayed, end-of-day or demonstration — and missing
data is reported as unavailable rather than estimated. Every score is returned with its full
decomposition.
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
    logger.info("%s ready on http://%s:%s", BRAND_PRODUCT, settings.host, settings.port)
    try:
        yield
    finally:
        shutdown_scheduler()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=f"{BRAND_PRODUCT} API",
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

    from backend.api.routes_admin import router as admin_router
    from backend.api.routes_api import router as api_router
    from backend.api.routes_auth import router as auth_router
    from backend.api.routes_billing import router as billing_router
    from backend.api.routes_gmg import router as gmg_router
    from backend.api.routes_pages import router as pages_router
    from backend.api.routes_public import router as public_router
    from backend.api.routes_workspace import router as workspace_router

    # Public marketing and legal pages first: they own "/".
    app.include_router(public_router)
    app.include_router(auth_router)
    app.include_router(billing_router)
    app.include_router(gmg_router)
    app.include_router(workspace_router)
    app.include_router(admin_router)
    app.include_router(api_router)
    # The original research terminal, kept for GMG's own analysts. It is mounted
    # under /terminal so it cannot shadow the customer-facing routes, and it is
    # restricted to administrators: it exposes the full research stack —
    # backtesting, paper trading, risk and thesis management — which is internal
    # tooling, not part of the subscription.
    from backend.api.auth_deps import require_admin

    app.include_router(pages_router, prefix="/terminal",
                       dependencies=[Depends(require_admin)])

    _install_error_handlers(app, settings)

    return app


def _install_error_handlers(app: FastAPI, settings) -> None:
    from backend.api.auth_deps import AccessDenied, CsrfError, RedirectException

    @app.exception_handler(RedirectException)
    async def redirect_handler(request: Request, exc: RedirectException):  # noqa: ANN001
        return RedirectResponse(exc.url, status_code=exc.status_code)

    @app.exception_handler(AccessDenied)
    async def access_denied(request: Request, exc: AccessDenied):  # noqa: ANN001
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.message}, status_code=403)
        return templates.TemplateResponse(
            request, "gmg/error.html",
            _gmg_error_context(settings, 403, exc.message), status_code=403,
        )

    @app.exception_handler(CsrfError)
    async def csrf_failed(request: Request, exc: CsrfError):  # noqa: ANN001
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return templates.TemplateResponse(
            request, "gmg/error.html",
            _gmg_error_context(settings, 400, str(exc)), status_code=400,
        )

    @app.exception_handler(404)
    async def not_found(request: Request, exc):  # noqa: ANN001
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        detail = getattr(exc, "detail", "The requested page does not exist.")
        return templates.TemplateResponse(
            request, "gmg/error.html",
            _gmg_error_context(settings, 404, detail), status_code=404,
        )

    @app.exception_handler(500)
    async def server_error(request: Request, exc):  # noqa: ANN001
        logger.exception("Unhandled error on %s", request.url.path)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Internal server error"}, status_code=500)
        return templates.TemplateResponse(
            request, "gmg/error.html",
            _gmg_error_context(
                settings, 500,
                "An unexpected error occurred. Our logs have recorded it.",
            ),
            status_code=500,
        )


def _gmg_error_context(settings, code: int, message: str) -> dict:
    """Context for a GMG error page.

    Deliberately avoids the database and the viewer lookup: an error page that
    itself needs a working database cannot render the error that broke it.
    """
    return {
        "settings": settings, "code": code, "message": message,
        "brand": {
            "company": BRAND_COMPANY, "product": BRAND_PRODUCT,
            "short": "GMG", "tagline": "Investment Intelligence for the Egyptian Market",
        },
        "year": date.today().year,
    }


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
