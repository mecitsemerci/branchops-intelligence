"""Push ingestion: ``POST /api/ingest/{source_id}`` (and the legacy ``POST /api/webhooks/jira`` alias).

Every request is bounded, authenticated by the source adapter, validated, persisted to the inbox and
acknowledged; no model work happens in the API process.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from branchops.api.dependencies import AppServices, services
from branchops.application.commands.ingest_webhook import (
    IngestIncidentEvent,
    IngestOutcome,
    InvalidWebhookPayloadError,
    parse_payload,
)
from branchops.application.interfaces.source_adapter import (
    PushSourceAdapter,
    SourceAuthError,
    UnknownSourceError,
)
from branchops.infrastructure.observability.telemetry import get_instruments

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["ingestion"])


async def _read_bounded(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "payload too large")
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid content-length") from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _push_adapter(svc: AppServices, source_id: str) -> PushSourceAdapter:
    try:
        return svc.sources.get_push(source_id)
    except UnknownSourceError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown or disabled source") from exc


async def _ingest(request: Request, svc: AppServices, source_id: str) -> JSONResponse:
    started = time.perf_counter()
    instruments = get_instruments()
    outcome_label = "error"
    try:
        adapter = _push_adapter(svc, source_id)
        content_type = request.headers.get("content-type", "application/json")
        if "json" not in content_type.lower():
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "expected application/json")
        body = await _read_bounded(request, svc.settings.webhook.max_body_bytes)
        try:
            mechanism = adapter.authenticate(
                body=body,
                headers=request.headers,
                query=request.query_params,
                client_host=request.client.host if request.client else None,
            )
        except SourceAuthError as exc:
            outcome_label = "unauthorized"
            logger.warning("webhook authentication failed", extra={"reason": str(exc), "source": source_id})
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized") from exc
        try:
            payload = parse_payload(body)
            command = IngestIncidentEvent(svc.db, svc.settings, svc.sources)
            result = await run_in_threadpool(
                command.execute,
                source_id,
                payload,
                correlation_id=request.state.correlation_id,
                headers=dict(request.headers),
                signed=mechanism == "hmac",
            )
        except InvalidWebhookPayloadError as exc:
            outcome_label = "invalid"
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid payload: {exc}") from exc
        outcome_label = result.outcome.value
        logger.info(
            "webhook received",
            extra={
                "outcome": result.outcome.value,
                "event_type": result.event_type or "",
                "auth": mechanism,
                "source": source_id,
            },
        )
        code = status.HTTP_202_ACCEPTED if result.outcome is IngestOutcome.ACCEPTED else status.HTTP_200_OK
        return JSONResponse(
            status_code=code,
            content={
                "status": result.outcome.value,
                "source": source_id,
                "event_id": result.event_id,
                "correlation_id": result.correlation_id,
                "reason": result.reason,
            },
        )
    except HTTPException as exc:
        if outcome_label == "error":
            outcome_label = f"http_{exc.status_code}"
        raise
    finally:
        attrs = {"outcome": outcome_label, "source": source_id}
        instruments.webhook_requests.add(1, attrs)
        instruments.webhook_latency_ms.record((time.perf_counter() - started) * 1000, attrs)


@router.post("/ingest/{source_id}", status_code=status.HTTP_202_ACCEPTED)
async def ingest(source_id: str, request: Request, svc: AppServices = Depends(services)) -> JSONResponse:
    return await _ingest(request, svc, source_id)


@router.post("/webhooks/jira", status_code=status.HTTP_202_ACCEPTED)
async def jira_webhook(request: Request, svc: AppServices = Depends(services)) -> JSONResponse:
    """Legacy URL registered in existing Jira webhook configurations; same as ``/api/ingest/jira``."""
    return await _ingest(request, svc, "jira")
