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


BASE = ROOT / "deploy" / "openshift" / "base"


def _docs() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(BASE.glob("*.yaml")):
        if path.name in ("kustomization.yaml", "branchops.yaml", "secret.example.yaml"):
            continue
        out.extend(d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d)
    return out


def _workloads() -> list[dict[str, Any]]:
    return [d for d in _docs() if d.get("kind") in ("Deployment", "Job")]


def test_restricted_security_context_and_limits_everywhere() -> None:
    """OpenShift restricted-v2: non-root (arbitrary UID), no privilege escalation, read-only root FS."""
    workloads = _workloads()
    assert {w["metadata"]["name"] for w in workloads} >= {
        "branchops-api",
        "branchops-worker",
        "branchops-dashboard",
        "branchops-mcp",
        "branchops-migrate",
    }
    for w in workloads:
        pod = w["spec"]["template"]["spec"]
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        assert "runAsUser" not in pod["securityContext"]  # the platform assigns the UID
        for c in pod["containers"]:
            sc = c["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False and sc["readOnlyRootFilesystem"] is True
            assert sc["capabilities"]["drop"] == ["ALL"]
            assert c["resources"]["requests"] and c["resources"]["limits"]
            assert not any("gpu" in k for k in c["resources"]["limits"])  # the model is served elsewhere


def test_probes_and_graceful_termination() -> None:
    config = yaml.safe_load((BASE / "branchops.yaml").read_text(encoding="utf-8"))
    for w in (d for d in _workloads() if d["kind"] == "Deployment"):
        pod = w["spec"]["template"]["spec"]
        containers = pod["containers"]
        assert any("readinessProbe" in c for c in containers) and any(
            "livenessProbe" in c for c in containers
        )
        name = w["metadata"]["name"]
        grace = pod.get("terminationGracePeriodSeconds", 30)
        if name == "branchops-worker":
            # In-flight analyses get worker.shutdown_grace_seconds, then are handed back: the pod outlives it.
            assert grace > config["worker"]["shutdown_grace_seconds"]
        else:
            app = next(c for c in containers if c["name"] != "oauth2-proxy")
            assert app["lifecycle"]["preStop"]["exec"]["command"][0] == "sleep"
            assert grace >= 30


def test_readiness_never_depends_on_the_model() -> None:
    """A model outage must not take pods out of service (ingestion continues; recovery handles analyses)."""
    for w in (d for d in _workloads() if d["kind"] == "Deployment"):
        for c in w["spec"]["template"]["spec"]["containers"]:
            probe = c.get("readinessProbe", {}).get("httpGet", {})
            assert "llm" not in probe.get("path", "") and "model" not in probe.get("path", "")
