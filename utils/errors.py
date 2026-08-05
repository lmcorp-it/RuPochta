"""
HTTP exception and error response helpers.
"""

from fastapi import HTTPException, status
from typing import Any, Dict, Optional


def error_bad_request(detail: str) -> HTTPException:
    """Return HTTP 400 Bad Request."""
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def error_unauthorized(detail: str = "Unauthorized") -> HTTPException:
    """Return HTTP 401 Unauthorized."""
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def error_forbidden(detail: str = "Forbidden") -> HTTPException:
    """Return HTTP 403 Forbidden."""
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def error_not_found(detail: str = "Not Found") -> HTTPException:
    """Return HTTP 404 Not Found."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def error_conflict(detail: str) -> HTTPException:
    """Return HTTP 409 Conflict."""
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def error_service_unavailable(detail: str) -> HTTPException:
    """Return HTTP 503 Service Unavailable."""
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


def error_too_many_requests(detail: str) -> HTTPException:
    """Return HTTP 429 Too Many Requests."""
    return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


def json_error_response(
    ok: bool = False,
    error: Optional[str] = None,
    detail: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Build a JSON error response object."""
    response = {"ok": ok}
    if error:
        response["error"] = error
    if detail:
        response["detail"] = detail
    response.update(extra)
    return response


def json_ok_response(
    ok: bool = True,
    **extra: Any,
) -> Dict[str, Any]:
    """Build a JSON success response object."""
    response = {"ok": ok}
    response.update(extra)
    return response
