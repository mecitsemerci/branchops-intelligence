#!/usr/bin/env python3
"""Demo data for the ownership and notification pages (development stack with dev auth only).

Creates teams through the admin REST API, imports applications/screens as CSV, and reports two incidents
through the MCP server's ``report_incident`` tool with app context, so that routing and (logged)
notifications happen. Idempotent: existing teams are kept, the CSV import updates in place and the
reports use fixed idempotency keys.

    .venv/bin/python scripts/seed_demo_ownership.py [--api-url URL] [--mcp-url URL]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

TEAMS: list[dict[str, Any]] = [
    {
        "key": "branch-ops",
        "name": "Şube Operasyon",
        "kind": "branch_ops",
        "email": "sube-operasyon@bank.example",
        "working_hours": "Mon-Fri 08:30-17:30",
    },
    {
        "key": "eft-team",
        "name": "EFT Ekibi",
        "email": "eft-ekibi@bank.example",
        "jira_project_key": "EFT",
        "working_hours": "Mon-Fri 08:30-17:30",
    },
    {"key": "crm-team", "name": "Müşteri Yönetimi Ekibi", "email": "crm@bank.example"},
    {"key": "card-team", "name": "Kart Sistemleri Ekibi", "email": "kart@bank.example"},
]
SCREENS_CSV = """application_key,screen_key,title,route_pattern,owner_team_key
eft,eft-new,Yeni EFT Gönderimi,/eft/new,eft-team
eft,eft-detail,EFT Detay,/eft/{id},eft-team
crm,customer-search,Müşteri Arama,/customers,crm-team
crm,customer-identity,Kimlik Doğrulama,/customers/{id}/identity,crm-team
cards,card-reader,Kart Okuyucu İşlemleri,/cards/reader,card-team
"""
REPORTS: list[tuple[str, dict[str, Any]]] = [
    (
        "demo-eft-1",
        {
            "title": "EFT gönderiminde EFT-4012 zaman aşımı",
            "description": "Yeni EFT ekranında gönder butonuna basınca 30 saniye sonra EFT-4012 hatası "
            "alınıyor. Şubede üç müşterinin havalesi gönderilemedi.",
            "branch_code": "1001",
            "application": "Gişe Ekranı",
            "business_impact": "Müşteri havaleleri bekliyor",
            "error_details": "EFT-4012 Gateway timeout",
            "app_context": {
                "app_id": "eft",
                "screen_id": "eft-new",
                "route": "/eft/new",
                "error_code": "EFT-4012",
                "environment": "prod",
            },
        },
    ),
    (
        "demo-crm-1",
        {
            "title": "Müşteri kimlik doğrulama ekranı açılmıyor",
            "description": "Kimlik doğrulama ekranında KPS-408 hatası veriliyor, hesap açılışı yapılamıyor.",
            "branch_code": "3160",
            "app_context": {
                "app_id": "crm",
                "screen_id": "customer-identity",
                "route": "/customers/42/identity",
                "error_code": "KPS-408",
            },
        },
    ),
]


def _request(method: str, url: str, body: bytes | None = None, content_type: str = "application/json") -> Any:
    if not url.startswith(("http://", "https://")):
        raise SystemExit(f"unsupported URL scheme: {url}")
    request = urllib.request.Request(  # noqa: S310 - scheme validated above
        url, data=body, method=method, headers={"Content-Type": content_type}
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - scheme validated above
        return json.loads(response.read() or b"null")


def seed_ownership(api_url: str) -> None:
    base = api_url.rstrip("/")
    existing = {t["key"] for t in _request("GET", f"{base}/api/ownership/teams")}
    for team in TEAMS:
        if team["key"] in existing:
            print(f"team {team['key']:12s} exists")
            continue
        _request("POST", f"{base}/api/admin/ownership/teams", json.dumps(team).encode())
        print(f"team {team['key']:12s} created")
    try:
        report = _request(
            "POST", f"{base}/api/admin/ownership/import", SCREENS_CSV.encode(), content_type="text/csv"
        )
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"CSV import failed: {exc.code} {exc.read().decode(errors='replace')}") from exc
    print(f"screens import: {json.dumps(report, ensure_ascii=False)[:200]}")


async def report_incidents(mcp_url: str) -> None:
    async with (
        streamablehttp_client(mcp_url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        for key, draft in REPORTS:
            result = await session.call_tool("report_incident", {"draft": draft, "idempotency_key": key})
            text = result.content[0].text if result.content else ""  # type: ignore[union-attr]
            print(f"report {key}: {'error ' if result.isError else ''}{text[:160]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-url", default="http://localhost:8080")
    parser.add_argument("--mcp-url", default="http://localhost:8095/mcp")
    args = parser.parse_args()
    seed_ownership(args.api_url)
    asyncio.run(report_incidents(args.mcp_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
