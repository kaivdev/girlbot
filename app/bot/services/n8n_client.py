from __future__ import annotations

"""HTTP client for n8n webhook endpoint."""

from contextlib import asynccontextmanager
from time import perf_counter
from typing import AsyncIterator

import httpx

from app.bot.schemas.n8n_io import N8nRequest, N8nResponse
from app.bot.services.metrics import metrics
from app.config.settings import get_settings
from app.bot.services.logging import get_logger


@asynccontextmanager
async def _client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        yield client


async def call_n8n(req: N8nRequest, *, trace_id: str | None = None) -> N8nResponse:
    """POST the request to n8n and return parsed response.

    Raises httpx.HTTPError on network errors.
    """

    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    if trace_id:
        headers["X-Trace-Id"] = trace_id
    if settings.openrouter_referrer:
        headers["Referer"] = settings.openrouter_referrer

    payload = req.model_dump(mode="json")
    start = perf_counter()
    logger = get_logger().bind(intent=req.intent)
    try:
        async with _client() as client:
            resp = await client.post(str(settings.n8n_webhook_url), json=payload, headers=headers)
            resp.raise_for_status()
            raw = resp.json()

            # Normalize possible n8n response shapes
            data = raw
            if isinstance(raw, list):
                if raw and isinstance(raw[0], dict):
                    first = raw[0]
                    data = first.get("json", first)
            elif isinstance(raw, dict):
                if "json" in raw and isinstance(raw["json"], dict):
                    data = raw["json"]
                elif "data" in raw and isinstance(raw["data"], dict):
                    data = raw["data"]

            n8n_resp = N8nResponse.model_validate(data)
            return n8n_resp
    finally:
        elapsed = perf_counter() - start
        metrics.observe("n8n_request_seconds", elapsed, labels={"intent": req.intent})
