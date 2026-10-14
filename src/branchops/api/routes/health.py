"""Kubernetes/OpenShift probes and Prometheus metrics."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.concurrency import run_in_threadpool

from branchops.api.dependencies import AppServices, services

router = APIRouter(tags=["health"])


@router.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/startup")
def startup(svc: AppServices = Depends(services)) -> dict[str, object]:
    return {"status": "ok", "templates": len(svc.registry.latest_published())}


@router.get("/health/ready")
async def ready(response: Response, svc: AppServices = Depends(services)) -> dict[str, str]:
    try:
        await run_in_threadpool(svc.db.ping)
    except Exception:
        response.status_code = 503
        return {"status": "database unavailable"}
    return {"status": "ready"}


@router.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
