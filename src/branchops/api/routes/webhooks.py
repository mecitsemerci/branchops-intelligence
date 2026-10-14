"""POST /api/webhooks/jira — authenticate, bound, validate, persist to inbox, acknowledge."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from branchops.api.dependencies import AppServices, services
from branchops.application.commands.ingest_webhook import (
    IngestJiraWebhook,
    IngestOutcome,
    InvalidWebhookPayloadError,
    parse_payload,
)
from branchops.infrastructure.jira.adapter import JiraWebhookAdapter
from branchops.infrastructure.observability.telemetry import get_instruments
from branchops.infrastructure.security.webhook_auth import WebhookAuthError, authenticate_webhook

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


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


@router.post("/jira", status_code=status.HTTP_202_ACCEPTED)
async def jira_webhook(request: Request, svc: AppServices = Depends(services)) -> JSONResponse:
    started = time.perf_counter()
    instruments = get_instruments()
    outcome_label = "error"
    try:
        content_type = request.headers.get("content-type", "application/json")
        if "json" not in content_type.lower():
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "expected application/json")
        body = await _read_bounded(request, svc.settings.webhook.max_body_bytes)
        try:
            mechanism = authenticate_webhook(
                body=body,
                headers=request.headers,
                query=request.query_params,
                client_host=request.client.host if request.client else None,
                settings=svc.settings.webhook,
                secrets=svc.settings.webhook_secrets(),
                query_tokens=svc.settings.webhook_query_tokens(),
            )
        except WebhookAuthError as exc:
            outcome_label = "unauthorized"
            logger.warning("webhook authentication failed", extra={"reason": str(exc)})
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized") from exc
        try:
            payload = parse_payload(body)
            command = IngestJiraWebhook(
                svc.db, svc.settings, JiraWebhookAdapter(svc.settings.jira.field_mapping)
            )
            result = await run_in_threadpool(
                command.execute,
                payload,
                correlation_id=request.state.correlation_id,
                delivery_id=request.headers.get("x-atlassian-webhook-identifier"),
            )
        except InvalidWebhookPayloadError as exc:
            outcome_label = "invalid"
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid payload: {exc}") from exc
        outcome_label = result.outcome.value
        logger.info(
            "webhook received",
            extra={"outcome": result.outcome.value, "event_type": result.event_type or "", "auth": mechanism},
        )
        code = status.HTTP_202_ACCEPTED if result.outcome is IngestOutcome.ACCEPTED else status.HTTP_200_OK
        return JSONResponse(
            status_code=code,
            content={
                "status": result.outcome.value,
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
        instruments.webhook_requests.add(1, {"outcome": outcome_label})
        instruments.webhook_latency_ms.record(
            (time.perf_counter() - started) * 1000, {"outcome": outcome_label}
        )
