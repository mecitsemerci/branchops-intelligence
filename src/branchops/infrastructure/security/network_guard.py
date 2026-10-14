"""Outbound endpoint guard enforcing the air-gapped deployment rule.

Every outbound URL the application is configured to call (LLM gateway, OTLP collector) is checked at
startup. Hosts must either match an allow-listed internal DNS suffix or be a private/loopback IP
literal. Public hosts are rejected even if the network would block them anyway, so that a configuration
mistake fails loudly instead of silently attempting an internet call.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from branchops.config.settings import NetworkSettings


class ExternalEndpointError(ValueError):
    pass


def assert_internal_url(url: str, network: NetworkSettings, *, purpose: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ExternalEndpointError(f"{purpose}: unsupported URL scheme '{parts.scheme}'")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ExternalEndpointError(f"{purpose}: URL has no host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if network.allow_private_ips and (ip.is_private or ip.is_loopback):
            return
        raise ExternalEndpointError(f"{purpose}: IP {host} is not a private address")
    for suffix in network.allowed_host_suffixes:
        s = suffix.lower()
        if host == s.lstrip(".") or host.endswith(s if s.startswith(".") else f".{s}") or host == s:
            return
    raise ExternalEndpointError(
        f"{purpose}: host '{host}' is not in network.allowed_host_suffixes; refusing external endpoint"
    )
