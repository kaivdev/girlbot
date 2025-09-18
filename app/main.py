from __future__ import annotations

"""FastAPI app entry: webhook, healthz, metrics, plus simple file upload/serve."""

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import PlainTextResponse, FileResponse
import structlog

from app.bot.loader import setup_bot
from app.bot.services.logging import configure_logging
from app.bot.services.metrics import metrics
from app.bot.webhook import router as webhook_router
from app.config.settings import get_settings


configure_logging(get_settings().log_level)
logger = structlog.get_logger()
logger.info("startup", n8n_webhook_url=str(get_settings().n8n_webhook_url))


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


# --- Simple upload/serve for voice/audio to be consumed by n8n ---
_UPLOAD_DIR = Path("/tmp/uploads")
_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)) -> dict:
    settings = get_settings()
    # Preserve a safe extension
    suffix = ""
    name = (file.filename or "upload.bin").lower()
    for ext in (
        # audio
        ".ogg", ".oga", ".mp3", ".m4a", ".wav", ".webm", ".amr",
        # images
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic",
    ):
        if name.endswith(ext):
            suffix = ext
            break
    fname = f"{uuid.uuid4().hex}{suffix}"
    fpath = _UPLOAD_DIR / fname

    # Save to disk in chunks
    with fpath.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)

    url = str(settings.public_base_url).rstrip("/") + f"/files/{fname}"
    return {"url": url, "filename": fname, "mime_type": file.content_type}


@app.get("/files/{fname}")
async def serve_file(fname: str):
    fpath = _UPLOAD_DIR / fname
    if not fpath.exists() or not fpath.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(str(fpath))
