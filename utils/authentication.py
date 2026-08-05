"""
Authentication and access control utilities.
"""

from typing import Any, Dict, Optional
from fastapi import Request


def get_client_ip(request: Request) -> str:
    """Real client IP as seen by the trusted reverse proxy.
    
    Prefer X-Real-IP (nginx sets it to $remote_addr — not client-appendable).
    Fall back to the LAST X-Forwarded-For hop: nginx uses
    $proxy_add_x_forwarded_for, which *appends* the real peer, so the rightmost
    entry is the trusted one. The leftmost entry is fully attacker-controlled
    and must never be used for authorization (it would defeat
    is_internal_client_ip).
    """
    real_ip = (request.headers.get("x-real-ip") or "").strip()
    if real_ip:
        return real_ip
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        hops = [p.strip() for p in xff.split(",") if p.strip()]
        if hops:
            return hops[-1]
    if request.client:
        return request.client.host
    return ""


def get_request_host(request: Request) -> str:
    """Extract Host from request headers, handling proxies and multiple values.
    
    Prefer X-Forwarded-Host (set by reverse proxy), then Host header,
    then fall back to request URL netloc. Takes first value if comma-separated.
    """
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
    if forwarded_host:
        return forwarded_host
    host = (request.headers.get("host") or "").split(",", 1)[0].strip()
    if host:
        return host
    return (request.url.netloc or "").strip()


def get_request_origin(request: Request) -> str:
    """Extract Origin header from request."""
    origin = request.headers.get("origin", "").split(",", 1)[0].strip()
    return origin


def get_request_referer(request: Request) -> str:
    """Extract Referer header from request."""
    return request.headers.get("referer", "").strip()


def is_request_https(request: Request) -> bool:
    """Check if request was made over HTTPS.
    
    Prefer X-Forwarded-Proto (set by reverse proxy), then fall back to
    request URL scheme. Takes first value if comma-separated.
    """
    proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    if proto:
        return proto == "https"
    return (request.url.scheme or "").lower() == "https"


def build_expected_origin(request: Request) -> str:
    """Build the expected origin URL for same-origin validation."""
    protocol = "https" if is_request_https(request) else "http"
    host = get_request_host(request)
    if not host:
        return ""
    return f"{protocol}://{host}"


def is_same_origin_request(request: Request, client_ip_check=None) -> bool:
    """Check if request has valid same-origin headers.
    
    Args:
        request: FastAPI Request object
        client_ip_check: Optional callable(client_ip: str) -> bool to check if IP is internal
    
    Returns True if:
    - Origin header matches expected origin, OR
    - Referer header matches expected origin (or is under it), OR
    - Request is from internal IP (if client_ip_check provided)
    """
    expected_host = get_request_host(request)
    if not expected_host:
        return False
    expected_origin = f"{'https' if is_request_https(request) else 'http'}://{expected_host}"
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
