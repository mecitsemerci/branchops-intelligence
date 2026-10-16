#!/usr/bin/env python3
"""Capture the README screenshots of the running dashboard with Playwright (Chromium, headless).

Run against a stack started with ``make up LLM_MODE=mock`` that has data (``make sim-once``, ``make seed``):

    .venv/bin/playwright install chromium        # once
    .venv/bin/python scripts/capture_screenshots.py [--dashboard-url URL] [--api-url URL] [--out DIR]

Every page is captured full page at 1440x900, device scale factor 2, after network idle plus 2 s for
NiceGUI to render. A page that shows the login/authorisation error or no content fails the run. Images
larger than ``--max-bytes`` are downscaled to 1x. ``screenshots.json`` records page, URL and timestamp.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from PIL import Image
from playwright.sync_api import Page, ViewportSize, sync_playwright

VIEWPORT: ViewportSize = {"width": 1440, "height": 900}
SCALE = 2
RENDER_WAIT_MS = 2000
LIVE_WAIT_MS = 120_000
PAGES: list[tuple[str, str]] = [
    ("01-overview.png", "/"),
    ("02-live-agents.png", "/live"),
    ("03-issues.png", "/issues"),
    ("04-issue-detail.png", "{issue}"),
    ("05-analytics.png", "/analytics"),
    ("06-executions.png", "/executions"),
    ("07-ownership-admin.png", "/yonetim"),
    ("08-notifications.png", "/bildirimler"),
]
# Shown by the dashboard instead of the page when the caller is not authorised (dashboard/components).
UNAUTHORISED = ("Yetkisiz erişim", "rolü gerekir")


def _get_json(url: str) -> Any:
    if not url.startswith(("http://", "https://")):
        raise SystemExit(f"unsupported URL scheme: {url}")
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - scheme validated above
        return json.loads(response.read())


def pick_issue(api_url: str) -> str:
    """First incident (most recently updated) whose latest analysis is COMPLETED or PARTIAL."""
    query = urlencode({"page_size": 100, "sort": "updated", "descending": "true"})
    page = _get_json(f"{api_url.rstrip('/')}/api/issues?{query}")
    for item in page.get("items", []):
        if item.get("analysis_status") in ("COMPLETED", "PARTIAL"):
            return str(item["incident_key"])
    raise SystemExit("no COMPLETED or PARTIAL analysis yet: run `make sim-once` / `make seed` and wait")


def issue_path(incident_key: str) -> str:
    return "/issues/" + quote(incident_key, safe=":/@")  # as dashboard.components.layout.issue_url


def _check_rendered(page: Page, name: str) -> None:
    text = page.inner_text("body")
    if any(marker in text for marker in UNAUTHORISED):
        raise SystemExit(f"{name}: the dashboard refused access (check security.auth_mode / dev auth)")
    if len(text.strip()) < 200:
        raise SystemExit(f"{name}: the page has almost no content ({len(text.strip())} characters)")


def _fit(path: Path, max_bytes: int) -> tuple[int, int, int]:
    """Downscale to 1x (and optimise) when the 2x capture is too large; returns width, height, bytes."""
    if path.stat().st_size > max_bytes:
        with Image.open(path) as img:
            small = img.resize((img.width // SCALE, img.height // SCALE), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            small.save(buffer, format="PNG", optimize=True)
        path.write_bytes(buffer.getvalue())
    with Image.open(path) as img:
        extrema = img.convert("L").getextrema()
        if extrema[0] == extrema[1]:
            raise SystemExit(f"{path.name}: the image is blank")
        return img.width, img.height, path.stat().st_size


def capture(dashboard_url: str, api_url: str, out: Path, issue_key: str | None, max_bytes: int) -> int:
    out.mkdir(parents=True, exist_ok=True)
    key = issue_key or pick_issue(api_url)
    base = dashboard_url.rstrip("/")
    manifest: list[dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport=VIEWPORT, device_scale_factor=SCALE, locale="tr-TR", timezone_id="Europe/Istanbul"
        )
        page = context.new_page()
        for name, path in PAGES:
            url = base + (issue_path(key) if path == "{issue}" else path)
            page.goto(url, wait_until="networkidle")
            if path == "/live":  # a finished-analysis card: "tamlık … · kalite … · … sn · … token"
                page.get_by_text(re.compile(r"tamlık .* · kalite .* token")).first.wait_for(
                    timeout=LIVE_WAIT_MS
                )
            page.wait_for_timeout(RENDER_WAIT_MS)
            _check_rendered(page, name)
            target = out / name
            page.screenshot(path=str(target), full_page=True)
            width, height, size = _fit(target, max_bytes)
            manifest.append(
                {
                    "page": name.removesuffix(".png"),
                    "file": name,
                    "url": url,
                    "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                    "width": width,
                    "height": height,
                    "bytes": size,
                }
            )
            print(f"{name:26s} {width}x{height} {size / 1024:.0f} KiB  {url}")
        browser.close()
    (out / "screenshots.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dashboard-url", default=os.environ.get("BASE_DASHBOARD_URL", "http://localhost:8090")
    )
    parser.add_argument("--api-url", default=os.environ.get("API_URL", "http://localhost:8080"))
    parser.add_argument("--out", type=Path, default=Path("docs/screenshots"))
    parser.add_argument("--issue-key", help="incident for the detail page (default: first analysed one)")
    parser.add_argument("--max-bytes", type=int, default=1_500_000)
    args = parser.parse_args()
    return capture(args.dashboard_url, args.api_url, args.out, args.issue_key, args.max_bytes)


if __name__ == "__main__":
    sys.exit(main())
