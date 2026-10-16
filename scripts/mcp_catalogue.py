#!/usr/bin/env python3
"""List the tools, resources and prompts of a running BranchOps MCP server (streamable HTTP).

    .venv/bin/python scripts/mcp_catalogue.py [--url http://localhost:8095/mcp] [--token TOKEN]

With dev auth (docker compose) no token is needed; otherwise pass an SSO bearer token.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _first_line(text: str | None) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


async def catalogue(url: str, token: str | None) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with (
        streamablehttp_client(url, headers=headers) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        info = await session.initialize()
        print(f"server  {info.serverInfo.name} {info.serverInfo.version or ''}".rstrip())
        tools = (await session.list_tools()).tools
        print(f"\ntools ({len(tools)})")
        for tool in sorted(tools, key=lambda t: t.name):
            print(f"  {tool.name:28s} {_first_line(tool.description)[:90]}")
        resources = (await session.list_resources()).resources
        templates = (await session.list_resource_templates()).resourceTemplates
        print(f"\nresources ({len(resources)} + {len(templates)} templates)")
        for res in sorted(resources, key=lambda r: str(r.uri)):
            print(f"  {res.uri!s:40s} {_first_line(res.description)[:78]}")
        for tpl in sorted(templates, key=lambda t: t.uriTemplate):
            print(f"  {tpl.uriTemplate:40s} {_first_line(tpl.description)[:78]}")
        prompts = (await session.list_prompts()).prompts
        print(f"\nprompts ({len(prompts)})")
        for prompt in sorted(prompts, key=lambda p: p.name):
            print(f"  {prompt.name:28s} {_first_line(prompt.description)[:90]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:8095/mcp")
    parser.add_argument("--token")
    args = parser.parse_args()
    return asyncio.run(catalogue(args.url, args.token))


if __name__ == "__main__":
    sys.exit(main())
