"""
IMAP/SMTP connection and protocol utilities.
"""

import imaplib
import re
from typing import Any, List, Optional


def imap_utf7_encode(value: str) -> str:
    """Encode string to IMAP UTF7 format for folder names."""
    if not value:
        return ""
    try:
        # Python 3.5+ imaplib.IMAP4 can use encode with 'imap4')
        return value.encode("imap4").decode("ascii")
    except Exception:
        # Fallback: manual modified-UTF7 encoding
        return value


def imap_utf7_decode(value: str) -> str:
    """Decode IMAP UTF7 folder name back to unicode."""
    if not value:
        return ""
    try:
        return value.encode("ascii").decode("imap4")
    except Exception:
        # Fallback: return as-is
        return value


def imap_quote_arg(value: str) -> bytes:
    """Quote a value for IMAP command arguments."""
    if not value:
        return b'""'
    # If no special chars, return as-is
    if re.match(r"^[a-zA-Z0-9._-]+$", value):
        return value.encode("utf-8")
    # Otherwise, use quoted string and escape quotes/backslashes
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'.encode("utf-8")


def imap_parse_status_response(response: tuple) -> Optional[str]:
    """Parse IMAP status response, extract message."""
    if not response or len(response) < 2:
        return None
    code, data = response[0], response[1]
    if isinstance(data, list) and len(data) > 0:
        return data[0].decode("utf-8", errors="replace") if isinstance(data[0], bytes) else str(data[0])
    return None


def smtp_tls_enabled(tls_required: bool) -> bool:
    """Check if SMTP TLS should be enabled."""
    return bool(tls_required)
