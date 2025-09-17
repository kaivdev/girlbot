from __future__ import annotations

"""HTTP client for n8n webhook endpoint."""

from contextlib import asynccontextmanager
from time import perf_counter
from typing import AsyncIterator

import httpx
from urllib.parse import urlparse

from app.bot.schemas.n8n_io import N8nRequest, N8nResponse
from app.bot.services.metrics import metrics
from app.config.settings import get_settings
from app.bot.services.logging import get_logger


def _is_ascii(value: str) -> bool:
    try:
        value.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


@asynccontextmanager
async def _client() -> AsyncIterator[httpx.AsyncClient]:
    # Increase timeout to accommodate slower LLM responses behind n8n
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        yield client


async def call_n8n(req: N8nRequest, *, trace_id: str | None = None) -> N8nResponse:
    """POST the request to n8n and return parsed response.

    Raises httpx.HTTPError on network errors.
    """

    settings = get_settings()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if trace_id:
        # ensure header value is ASCII-safe
        if _is_ascii(trace_id):
            headers["X-Trace-Id"] = trace_id
        else:
            get_logger().warning("skip_trace_id_non_ascii")
    if settings.openrouter_referrer:
        ref = settings.openrouter_referrer.strip()
        if _is_ascii(ref):
            # allow domain or full URL; normalize to URL
            parsed = urlparse(ref if ref.startswith("http") else f"https://{ref}")
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                headers["Referer"] = parsed.geturl()
            else:
                get_logger().warning("skip_referer_invalid", value=ref)
        else:
            get_logger().warning("skip_referer_non_ascii", value_preview=ref[:32])

    payload = req.model_dump(mode="json")
    start = perf_counter()
    logger = get_logger().bind(intent=req.intent)
    try:
        async with _client() as client:
            logger.info("n8n_request_start", url=str(settings.n8n_webhook_url))
            resp = await client.post(str(settings.n8n_webhook_url), json=payload, headers=headers)
            resp.raise_for_status()

            # Guard against empty bodies and non-JSON replies
            content_type = resp.headers.get("content-type", "")
            if not resp.content:
                logger.warning(
                    "n8n_empty_body",
                    status=resp.status_code,
                    content_type=content_type,
                )
                raise httpx.HTTPError("Empty response body from n8n")

            try:
                raw = resp.json()
            except Exception:
                # Log brief preview to help diagnostics
                logger.warning(
                    "n8n_bad_json",
                    status=resp.status_code,
                    content_type=content_type,
                    preview=resp.text[:200],
                )
                raise

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

            # Successful parse
            elapsed_ms = int((perf_counter() - start) * 1000)
            logger.info("n8n_request_ok", status=resp.status_code, elapsed_ms=elapsed_ms)
            n8n_resp = N8nResponse.model_validate(data)
            return n8n_resp
    finally:
        elapsed = perf_counter() - start
        metrics.observe("n8n_request_seconds", elapsed, labels={"intent": req.intent})
