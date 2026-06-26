"""API authentication helpers."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AuthDecision:
    allowed: bool
    status_code: int
    message: str


def parse_api_keys(raw_keys: str | None) -> list[str]:
    """Parse comma-separated API keys, ignoring empty placeholder values."""
    keys: list[str] = []
    for raw in (raw_keys or "").split(","):
        key = raw.strip()
        if not key or key.startswith("your_"):
            continue
        keys.append(key)
    return keys


def extract_api_key(headers: Any) -> str:
    """Extract an API key from X-API-Key or Authorization: Bearer."""
    x_api_key = headers.get("x-api-key") if headers else None
    if x_api_key:
        return str(x_api_key).strip()

    authorization = headers.get("authorization") if headers else None
    if not authorization:
        return ""
    scheme, _, value = str(authorization).partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return ""
    return value.strip()


def authorize_api_request(settings: Any, headers: Any) -> AuthDecision:
    """Validate request credentials against configured API keys."""
    if not bool(getattr(settings, "API_AUTH_ENABLED", True)):
        return AuthDecision(True, 200, "auth disabled")

    valid_keys = parse_api_keys(getattr(settings, "API_KEYS", ""))
    if not valid_keys:
        return AuthDecision(
            False,
            503,
            "API authentication is enabled but no API_KEYS are configured",
        )

    provided = extract_api_key(headers)
    if not provided:
        return AuthDecision(False, 401, "Missing API key")

    for valid_key in valid_keys:
        if hmac.compare_digest(provided, valid_key):
            return AuthDecision(True, 200, "authorized")

    return AuthDecision(False, 401, "Invalid API key")
