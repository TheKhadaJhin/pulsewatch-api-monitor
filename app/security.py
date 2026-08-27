"""Optional constant-time API key authentication for administrative routes."""

from __future__ import annotations

import ipaddress
import secrets
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings

_ADMIN_API_KEY = APIKeyHeader(
    name="X-API-Key",
    scheme_name="AdminApiKey",
    description="Administrative key configured with PULSEWATCH_ADMIN_API_KEY.",
    auto_error=False,
)
_ADMIN_BEARER = HTTPBearer(
    scheme_name="AdminBearer",
    description="The administrative API key supplied as a Bearer credential.",
    auto_error=False,
)


def validate_target(url: str, *, allow_private_hosts: bool) -> None:
    """Reject obvious server-side request forgery targets by default."""

    hostname = urlsplit(url).hostname
    if hostname is None:
        raise ValueError("Target URL has no hostname")
    if allow_private_hosts:
        return

    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        raise ValueError("Localhost targets are disabled")
    if normalized.endswith(".local") or normalized.endswith(".internal"):
        raise ValueError("Private network targets are disabled")

    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("Private or reserved IP targets are disabled")


class AdminAuthenticator:
    def __init__(self, settings: Settings) -> None:
        self.expected_key = settings.admin_api_key

    async def __call__(
        self,
        request: Request,
        api_key: Annotated[str | None, Security(_ADMIN_API_KEY)],
        bearer: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_ADMIN_BEARER),
        ],
    ) -> None:
        if self.expected_key is None:
            request.state.actor = "local-admin"
            return

        candidates = [api_key]
        if bearer is not None and bearer.scheme.lower() == "bearer":
            candidates.append(bearer.credentials)
        valid = any(
            candidate is not None
            and secrets.compare_digest(candidate, self.expected_key)
            for candidate in candidates
        )
        if not valid:
            raise HTTPException(
                status_code=401,
                detail="A valid administrative API key is required.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.actor = "api-key-admin"
