from __future__ import annotations

"""FastAPI app entry: webhook, healthz, metrics."""

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
import structlog

from app.bot.loader import setup_bot
from app.bot.services.logging import configure_logging
from app.bot.services.metrics import metrics
from app.bot.webhook import router as webhook_router
from app.config.settings import get_settings


configure_logging(get_settings().log_level)
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_bot(app)
    yield
    # Graceful shutdown is handled by FastAPI; APScheduler stops with loop


app = FastAPI(lifespan=lifespan)
app.include_router(webhook_router)


@app.middleware("http")
async def add_trace_id(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.clear_contextvars()
    return response


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics_endpoint() -> str:
    return metrics.to_prometheus()

