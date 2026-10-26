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


@app.middleware("http")
async def security_headers(request: Request, call_next):
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
        return RedirectResponse(exc.headers["Location"], status_code=303)
    from fastapi.exception_handlers import http_exception_handler
    return await http_exception_handler(request, exc)


@app.exception_handler(IntegrationError)
async def integration_errors(request: Request, exc: IntegrationError):
    """A missing key or failing provider is reported as what it is, with the fix, never papered over."""
    return JSONResponse({"detail": exc.message, "integration": exc.integration, "code": exc.code,
                         "recovery": exc.recovery}, status_code=424)


@app.get("/healthz")
async def healthz():
    s = get_settings()
    return {"ok": True, "env": s.app_env, "llm_mode": s.effective_llm_mode, "fixtures": s.fixtures_allowed}
