#!/usr/bin/env python3
"""Capture the README screenshots of the running dashboard with Playwright (Chromium, headless).

Run against a stack started with ``make up LLM_MODE=mock`` that has data (``make sim-once``, ``make seed``):

    .venv/bin/playwright install chromium        # once
    .venv/bin/python scripts/capture_screenshots.py [--dashboard-url URL] [--api-url URL] [--out DIR]

Every page is captured full page at 1440x900, device scale factor 2, after network idle plus 2 s for
NiceGUI to render. A page that shows the login/authorisation error or no content fails the run. Images
larger than ``--max-bytes`` are downscaled to 1x. ``screenshots<suffix>.json`` records page, URL and
timestamp.

Options for a real-model run (``make screenshots-vllm``): ``--suffix=-vllm`` names the files
``02-live-agents-vllm.png`` …; ``--pages`` selects a subset of the page keys below (the default set is the
original eight; ``issue-similar`` and ``issue-investigation`` are opt-in); ``--prefer-investigated`` picks,
for the detail pages, an incident whose latest analysis ran the investigation step to completion
(preferably one where the model called tools).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from PIL import Image
from playwright.sync_api import FloatRect, Page, ViewportSize, sync_playwright

VIEWPORT: ViewportSize = {"width": 1440, "height": 900}
SCALE = 2
RENDER_WAIT_MS = 2000
LIVE_WAIT_MS = 120_000
ISSUE = "{issue}"
SIMILAR_TAB = re.compile(r"benzer kayıtlar", re.IGNORECASE)
INVESTIGATION_HEADING = "Geçmiş incelemesi"  # dashboard/pages/issues.py: _investigation_panel


@dataclass(frozen=True)
class Shot:
    key: str  # --pages name
    stem: str  # file name without suffix and extension
    path: str  # dashboard path, or ISSUE for the selected incident
    tab: re.Pattern[str] | None = None  # tab to open before the screenshot
    section: str | None = None  # crop to the section that starts with this heading (inside ``tab``)
    default: bool = True  # part of the default set (`make screenshots`)


PAGES: list[Shot] = [
    Shot("overview", "01-overview", "/"),
    Shot("live", "02-live-agents", "/live"),
    Shot("issues", "03-issues", "/issues"),
    Shot("issue-detail", "04-issue-detail", ISSUE),
    # Investigation findings are rendered inside the "Benzer kayıtlar" tab, below the similar incidents.
    Shot("issue-similar", "04b-issue-similar", ISSUE, tab=SIMILAR_TAB, default=False),
    Shot(
        "issue-investigation",
        "04c-issue-investigation",
        ISSUE,
        tab=SIMILAR_TAB,
        section=INVESTIGATION_HEADING,
        default=False,
    ),
    Shot("analytics", "05-analytics", "/analytics"),
    Shot("executions", "06-executions", "/executions"),
    Shot("ownership-admin", "07-ownership-admin", "/yonetim"),
    Shot("notifications", "08-notifications", "/bildirimler"),
]
# Shown by the dashboard instead of the page when the caller is not authorised (dashboard/components).
UNAUTHORISED = ("Yetkisiz erişim", "rolü gerekir")


def _get_json(url: str) -> Any:
    if not url.startswith(("http://", "https://")):
        raise SystemExit(f"unsupported URL scheme: {url}")
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - scheme validated above
        return json.loads(response.read())


def _analysed_keys(api_url: str) -> list[str]:
    """Incidents (most recently updated first) whose latest analysis is COMPLETED or PARTIAL."""
    query = urlencode({"page_size": 200, "sort": "updated", "descending": "true"})
    page = _get_json(f"{api_url.rstrip('/')}/api/issues?{query}")
    return [
        str(item["incident_key"])
        for item in page.get("items", [])
        if item.get("analysis_status") in ("COMPLETED", "PARTIAL")
    ]


def _investigated(api_url: str, key: str) -> tuple[bool, int, int] | None:
    """(made tool calls, findings, tool calls) when the latest analysis ran the investigation to
    COMPLETED, else None. The model may conclude without a tool call when the candidates suffice."""
    detail = _get_json(f"{api_url.rstrip('/')}/api{issue_path(key)}")
    investigation = (detail.get("latest_analysis") or {}).get("investigation") or {}
    if investigation.get("status") != "COMPLETED":
        return None
    calls = len(investigation.get("tool_calls") or [])
    return calls > 0, len(investigation.get("findings") or []), calls


def pick_issue(api_url: str, *, prefer_investigated: bool = False) -> str:
    """First analysed incident; with ``prefer_investigated`` the one whose latest analysis completed the
    investigation step (with tool calls first, then most findings and tool calls), falling back with a
    warning."""
    keys = _analysed_keys(api_url)
    if not keys:
        raise SystemExit("no COMPLETED or PARTIAL analysis yet: run `make sim-once` / `make seed` and wait")
    if prefer_investigated:
        ranked = [(score, key) for key in keys if (score := _investigated(api_url, key)) is not None]
        if ranked:
            return max(ranked, key=lambda r: r[0])[1]
        print("warning: no analysis completed the investigation step; using the first analysed incident")
    return keys[0]


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


def _open_tab(page: Page, shot: Shot) -> None:
    if shot.tab is not None:
        page.get_by_role("tab", name=shot.tab).first.click()
        page.wait_for_timeout(RENDER_WAIT_MS)


def _section_clip(page: Page, heading: str) -> FloatRect | None:
    """Page rectangle from ``heading`` to the end of its container (the open tab panel), or None."""
    label = page.get_by_text(heading, exact=True)
    if label.count() == 0:
        return None
    page.evaluate("window.scrollTo(0, 0)")
    box: list[float] = label.first.evaluate(
        """el => {
            const top = el.getBoundingClientRect(), panel = el.parentElement.getBoundingClientRect();
            return [panel.left, top.top + window.scrollY - 16, panel.width, panel.bottom - top.top + 32];
        }"""
    )
    return {"x": box[0], "y": max(box[1], 0), "width": box[2], "height": box[3]}


def capture(
    dashboard_url: str,
    api_url: str,
    out: Path,
    issue_key: str | None,
    max_bytes: int,
    *,
    suffix: str = "",
    pages: list[str] | None = None,
    prefer_investigated: bool = False,
) -> int:
    known = {s.key for s in PAGES}
    unknown = sorted(set(pages or []) - known)
    if unknown:
        raise SystemExit(f"unknown --pages {', '.join(unknown)}; choose from {', '.join(sorted(known))}")
    shots = [s for s in PAGES if (s.key in pages if pages else s.default)]
    out.mkdir(parents=True, exist_ok=True)
    needs_issue = any(s.path == ISSUE for s in shots)
    key = issue_key or (pick_issue(api_url, prefer_investigated=prefer_investigated) if needs_issue else "")
    base = dashboard_url.rstrip("/")
    manifest: list[dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport=VIEWPORT, device_scale_factor=SCALE, locale="tr-TR", timezone_id="Europe/Istanbul"
        )
        page = context.new_page()
        for shot in shots:
            name = f"{shot.stem}{suffix}.png"
            url = base + (issue_path(key) if shot.path == ISSUE else shot.path)
            page.goto(url, wait_until="networkidle")
            if shot.path == "/live":  # a finished-analysis card: "tamlık … · kalite … · … sn · … token"
                page.get_by_text(re.compile(r"tamlık .* · kalite .* token")).first.wait_for(
                    timeout=LIVE_WAIT_MS
                )
            page.wait_for_timeout(RENDER_WAIT_MS)
            _check_rendered(page, name)
            _open_tab(page, shot)
            target = out / name
            if shot.section is not None:
                clip = _section_clip(page, shot.section)
                if clip is None:
                    print(f"warning: {name}: no '{shot.section}' section on {url}; skipped")
                    continue
                page.screenshot(path=str(target), full_page=True, clip=clip)
            else:
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
    manifest_file = out / f"screenshots{suffix}.json"
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
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
    parser.add_argument("--suffix", default="", help="appended to every file name, e.g. -vllm")
    parser.add_argument(
        "--pages",
        help="comma-separated page keys (default: the original eight): " + ", ".join(s.key for s in PAGES),
    )
    parser.add_argument(
        "--prefer-investigated",
        action="store_true",
        help="detail pages: an incident whose latest analysis completed the investigation step",
    )
    args = parser.parse_args()
    pages = [p.strip() for p in args.pages.split(",") if p.strip()] if args.pages else None
    return capture(
        args.dashboard_url,
        args.api_url,
        args.out,
        args.issue_key,
        args.max_bytes,
        suffix=args.suffix,
        pages=pages,
        prefer_investigated=args.prefer_investigated,
    )


if __name__ == "__main__":
    sys.exit(main())
