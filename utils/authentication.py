"""
Authentication and access control utilities.
"""

from typing import Any, Dict, Optional
from fastapi import Request


def get_client_ip(request: Request) -> str:
    """Extract client IP from request headers, handling proxies."""
    # Check X-Forwarded-For first (for proxied requests)
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    # Fall back to client host
    return request.client.host if request.client else ""


def get_request_host(request: Request) -> str:
    """Extract Host header from request."""
    return request.headers.get("host", "").strip()


def get_request_origin(request: Request) -> str:
    """Extract Origin header from request."""
    origin = request.headers.get("origin", "").split(",")[0].strip()
    return origin


def get_request_referer(request: Request) -> str:
    """Extract Referer header from request."""
    return request.headers.get("referer", "").strip()


def is_request_https(request: Request) -> bool:
    """Check if request was made over HTTPS."""
    # Check for X-Forwarded-Proto first
    proto = request.headers.get("x-forwarded-proto", "").strip().lower()
    if proto:
        return proto == "https"
    # Fall back to request URL scheme
    return request.url.scheme == "https"


def build_expected_origin(request: Request) -> str:
    """Build the expected origin URL for same-origin validation."""
    protocol = "https" if is_request_https(request) else "http"
    host = get_request_host(request)
    if not host:
        return ""
    return f"{protocol}://{host}"


def is_same_origin_request(request: Request) -> bool:
    """Check if request has valid same-origin headers."""
    expected_origin = build_expected_origin(request)
    if not expected_origin:
        return False
    
    origin = get_request_origin(request)
    if origin:
        return origin == expected_origin
    
    referer = get_request_referer(request)
    if referer:
        return referer == expected_origin or referer.startswith(expected_origin + "/")
    
    return False


def is_method_mutating(method: str) -> bool:
    """Check if HTTP method is mutating (not safe)."""
    return method.upper() not in {"GET", "HEAD", "OPTIONS"}
