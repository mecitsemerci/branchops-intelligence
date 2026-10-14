"""Static checks backing the air-gapped deployment claims."""

from __future__ import annotations

import re
from pathlib import Path

import nicegui

from tests.helpers import ROOT, make_settings

PUBLIC_URL_RE = re.compile(
    r"https?://(?!localhost|127\.0\.0\.1|[\w.-]+\.(?:internal|svc|local)\b)[\w.-]+\.[a-z]{2,}", re.I
)
CDN_RE = re.compile(r"fonts\.googleapis|fonts\.gstatic|cdn\.jsdelivr|unpkg\.com|cdnjs\.cloudflare", re.I)


def test_no_public_urls_in_application_code() -> None:
    offenders = []
    for path in (ROOT / "src").rglob("*.py"):
        for match in PUBLIC_URL_RE.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
    assert offenders == []


def test_nicegui_assets_are_bundled() -> None:
    pkg = Path(nicegui.__file__).parent
    offenders = [
        str(p.relative_to(pkg))
        for folder in ("templates", "static")
        for p in (pkg / folder).rglob("*")
        if p.is_file()
        and p.suffix in {".html", ".css", ".js"}
        and CDN_RE.search(p.read_text(errors="ignore"))
    ]
    assert offenders == []


def test_api_docs_disabled_because_they_use_a_cdn() -> None:
    from branchops.api.app import create_app
    from branchops.container import Container

    settings = make_settings()
    container = Container.__new__(Container)  # no DB connection needed to inspect routes
    container.settings = settings
    container.db = None  # type: ignore[assignment]
    container.registry = None  # type: ignore[assignment]
    container._closeables = []
    app = create_app(settings, container)
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/docs" not in paths
    assert "/redoc" not in paths


def test_default_settings_have_no_external_exporters() -> None:
    settings = make_settings()
    assert settings.observability.otlp_endpoint is None
    assert settings.llm.base_url is None
