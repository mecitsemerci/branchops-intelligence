"""Static checks on the OpenShift manifests."""

from __future__ import annotations

from typing import Any

import yaml

from tests.helpers import ROOT

API_MANIFEST = ROOT / "deploy" / "openshift" / "base" / "api.yaml"


def _container(name: str) -> dict[str, Any]:
    for doc in yaml.safe_load_all(API_MANIFEST.read_text(encoding="utf-8")):
        if doc and doc.get("kind") == "Deployment":
            for c in doc["spec"]["template"]["spec"]["containers"]:
                if c["name"] == name:
                    return c
    raise AssertionError(f"container {name!r} not found in {API_MANIFEST}")


def test_push_ingest_endpoints_bypass_sso() -> None:
    """Jira/ServiceNow cannot do SSO: ingest routes skip oauth2-proxy (they authenticate per source)."""
    args = _container("oauth2-proxy")["args"]
    routes = {a.removeprefix("--skip-auth-route=") for a in args if a.startswith("--skip-auth-route=")}
    assert "POST=^/api/webhooks/jira$" in routes
    assert "POST=^/api/ingest/[a-z0-9_-]+$" in routes
