"""Input validation helpers shared by API entry points."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


class InputValidationError(ValueError):
    """Raised when user-controlled input fails a blocking safety check."""


def validate_public_http_url(value: str, field_name: str = "url") -> str:
    """Validate a user-controlled remote URL before downstream fetching.

    This is a baseline SSRF guard. Production deployments should additionally
    enforce outbound-network policy and re-check resolved IPs after redirects.
    """
    if not value or not value.strip():
        raise InputValidationError(f"{field_name} 不能为空")

    normalized = value.strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        raise InputValidationError(f"{field_name} 仅支持 http/https URL")
    if not parsed.hostname:
        raise InputValidationError(f"{field_name} 缺少有效域名")
    if parsed.username or parsed.password:
        raise InputValidationError(f"{field_name} 不允许在 URL 中携带用户名或密码")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise InputValidationError(f"{field_name} 不允许访问本机地址")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return normalized

    if not address.is_global:
        raise InputValidationError(f"{field_name} 不允许访问私有、回环或保留地址")

    return normalized
