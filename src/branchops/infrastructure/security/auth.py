"""User authentication via the OpenShift oauth-proxy sidecar and role mapping.

In ``oauth_proxy`` mode the application trusts identity headers **only** when the TCP peer is one of
``trusted_proxy_hosts`` (the sidecar on the pod's loopback interface). The application container binds to
loopback in that deployment, and a NetworkPolicy blocks direct access, so headers cannot be spoofed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from branchops.config.settings import SecuritySettings
from branchops.domain.enums import Role

_ROLE_RANK = {Role.VIEWER: 0, Role.ANALYST: 1, Role.ADMIN: 2}


class AuthenticationError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    username: str
    roles: frozenset[Role]

    def has(self, required: Role) -> bool:
        return any(_ROLE_RANK[r] >= _ROLE_RANK[required] for r in self.roles)


def resolve_principal(
    headers: Mapping[str, str], client_host: str | None, settings: SecuritySettings
) -> Principal:
    if settings.auth_mode == "dev":
        return Principal(settings.dev_user, frozenset(settings.dev_roles))
    if client_host not in settings.trusted_proxy_hosts:
        raise AuthenticationError("request did not come through the authentication proxy")
    lower = {k.lower(): v for k, v in headers.items()}
    user = (lower.get(settings.user_header.lower()) or "").strip()
    if not user:
        raise AuthenticationError("missing authenticated user header")
    groups = {g.strip() for g in (lower.get(settings.groups_header.lower()) or "").split(",") if g.strip()}
    roles = frozenset(role for role, names in settings.role_groups.items() if groups & set(names))
    if not roles:
        raise AuthenticationError("user is not a member of any BranchOps group")
    return Principal(user[:200], roles)
