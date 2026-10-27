"""Growth OS app. Run: uvicorn app.main:app --reload"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.agents.orchestrator.engine import get_orchestrator
from app.api.routes import router as api_router
from app.bootstrap import init_database
from app.core.config import ConfigError, get_settings
from app.db.session import get_db
from app.integrations.errors import IntegrationError
from app.services.approvals import recover_interrupted_sends
from app.web.routes import router as web_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("growthos")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    for w in s.validate_runtime():          # raises ConfigError on fatal misconfiguration: the app refuses to start
        log.warning("config: %s", w)
    await init_database(get_db())
    interrupted = await recover_interrupted_sends()
    resumed = await get_orchestrator().resume_incomplete()
    log.info("Growth OS up: env=%s llm_mode=%s, resumed %d run(s), %d interrupted send(s) released",
             s.app_env, s.effective_llm_mode, resumed, interrupted)
    yield
    await get_db().dispose()


app = FastAPI(title="Growth OS", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")
app.include_router(api_router)
app.include_router(web_router)


UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def cross_site(request: Request) -> str | None:
    """CSRF defence for cookie sessions: a state-changing browser request must come from this site.

    Browsers send Origin on every cross-site POST (Referer as a fallback). If either is present and names another
    site (or 'null'), the request is refused before any route runs. Bearer-token API calls carry no ambient
    credentials, so they are not subject to CSRF and skip the check. SameSite=Lax cookies are the second layer."""
    if request.method not in UNSAFE or request.headers.get("authorization", "").lower().startswith("bearer "):
        return None
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    if not source:
        return None
    from urllib.parse import urlparse
    host = urlparse(source).netloc if source != "null" else "null"
    return None if host in {request.url.netloc, urlparse(get_settings().app_base_url).netloc} else host


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """One middleware layer (each layer costs latency): refuse cross-site writes, then add security headers."""
    if (bad := cross_site(request)) is not None:
        log.warning("refused cross-site %s %s from %s", request.method, request.url.path, bad)
        resp = JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
    else:
        resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if get_settings().cookie_secure:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


@app.exception_handler(HTTPException)
async def http_errors(request: Request, exc: HTTPException):
    if exc.status_code == 303 and exc.headers and "Location" in exc.headers:
        resp = RedirectResponse(exc.headers["Location"], status_code=303)
        if exc.headers["Location"] == "/login":
            from app.api.deps import COOKIE
            resp.delete_cookie(COOKIE)       # a stale or revoked session must not linger and loop
        return resp
    if exc.status_code in (403, 404) and not request.url.path.startswith("/api/"):
        return await friendly_error(request, exc)
    from fastapi.exception_handlers import http_exception_handler
    return await http_exception_handler(request, exc)


async def friendly_error(request: Request, exc: HTTPException):
    """Browser pages get a page that says what is missing; HTMX requests get a notice the page shows in place."""
    from fastapi.responses import HTMLResponse
    from markupsafe import escape
    detail = str(exc.detail or ("You don't have access to this." if exc.status_code == 403 else "Not found."))
    if request.headers.get("hx-request"):
        return HTMLResponse(f'<div class="flash bad">{escape(detail)}</div>', status_code=exc.status_code)
    from app.api.deps import principal_from_request
    from app.web.routes import render
    me = await principal_from_request(request)
    return await render(request, "error.html", me, status_code=exc.status_code, code=exc.status_code, detail=detail,
                        bare=me is None)


@app.exception_handler(IntegrationError)
async def integration_errors(request: Request, exc: IntegrationError):
    """A missing key or failing provider is reported as what it is, with the fix, never papered over."""
    return JSONResponse({"detail": exc.message, "integration": exc.integration, "code": exc.code,
                         "recovery": exc.recovery}, status_code=424)


@app.get("/healthz")
async def healthz():
    s = get_settings()
    return {"ok": True, "env": s.app_env, "llm_mode": s.effective_llm_mode, "fixtures": s.fixtures_allowed,
            "database": s.database_url.split(":", 1)[0].split("+")[0], "ephemeral_storage": s.ephemeral_storage}
