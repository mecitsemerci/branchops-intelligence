#!/usr/bin/env python3
"""Render terminal output (stdin) as a terminal-style PNG for the README, via rich (SVG) and Playwright.

    make mcp-health | .venv/bin/python scripts/render_terminal.py --title "make mcp-health" --out x.png

The command line(s) shown in the title bar are given with --title; the output is read from stdin. The SVG
is rendered by headless Chromium (fonts: the system monospace font, nothing is downloaded).
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright
from rich.console import Console
from rich.text import Text

WIDTH = 132  # terminal columns


def render(title: str, text: str, out: Path) -> None:
    console = Console(record=True, width=WIDTH, file=io.StringIO())
    console.print(Text(text.rstrip("\n")))
    svg = console.export_svg(title=title)
    out.parent.mkdir(parents=True, exist_ok=True)
    html = f"<html><body style='margin:0;background:#ffffff'>{svg}</body></html>"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.route("http*://**", lambda route: route.abort())  # rich's web font: use the local monospace
        page.set_content(html)
        page.locator("svg").first.screenshot(path=str(out))
        browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--title", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    text = sys.stdin.read()
    if not text.strip():
        raise SystemExit("no output to render")
    render(args.title, text, args.out)
    print(f"{args.out} ({args.out.stat().st_size / 1024:.0f} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
