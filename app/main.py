"""Growth OS app. Run: uvicorn app.main:app --reload"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.agents.orchestrator.engine import get_orchestrator
from app.api.routes import router as api_router
from app.core.config import get_settings
from app.db.session import get_db
from app.seed import seed
from app.web.routes import router as web_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("growthos")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await seed(get_db())  # no-op when tenants exist
    resumed = await get_orchestrator().resume_incomplete()
    log.info("Growth OS up: llm_mode=%s, resumed %d run(s)", get_settings().effective_llm_mode, resumed)
    yield
    await get_db().dispose()


app = FastAPI(title="Growth OS", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")
app.include_router(api_router)
app.include_router(web_router)


@app.exception_handler(HTTPException)
async def http_errors(request: Request, exc: HTTPException):
    if exc.status_code == 303 and exc.headers and "Location" in exc.headers:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    from fastapi.exception_handlers import http_exception_handler
    return await http_exception_handler(request, exc)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "llm_mode": get_settings().effective_llm_mode}
