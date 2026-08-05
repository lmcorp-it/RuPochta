"""
Validation utilities for email, domain, IP addresses, and sessions.
"""

import ipaddress
import re
from typing import Any


def is_valid_email(email: Any) -> bool:
    """Check if string looks like a valid email address."""
    value = str(email or "").strip().lower()
    if not value or "@" not in value:
        return False
    # Simple check: local@domain
    local, domain = value.rsplit("@", 1)
    return bool(local and domain and "." in domain)


def is_valid_domain(domain: Any) -> bool:
    """Check if string is a valid domain name."""
    value = str(domain or "").strip().lower()
    if not value:
        return False
    # Simple check: must have at least one dot, alphanumeric and hyphens
    if "." not in value:
        return False
    return bool(re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$", value))


def is_valid_ipaddress(ip: Any) -> bool:
    """Check if string is a valid IPv4 or IPv6 address."""
    try:
        ipaddress.ip_address(str(ip or ""))
        return True
    except (ValueError, TypeError):
        return False


def is_internal_client_ip(ip: Any) -> bool:
    """Check if IP is in the internal/private range (127.0.0.1, ::1, 10.0.0.0/8, etc.)."""
    try:
        addr = ipaddress.ip_address(str(ip or ""))
        return addr.is_private or addr.is_loopback
    except (ValueError, TypeError):
        return False


def is_valid_dn(dn: Any) -> bool:
    """Check if string looks like a valid LDAP Distinguished Name."""
    value = str(dn or "").strip()
    if not value:
        return False
    # Must have CN= or similar DN components
    return bool(re.search(r"(CN|OU|DC|O|C|ST|L)=", value, re.IGNORECASE))


def extract_email_domain(email: Any) -> str:
    """Extract domain from email address."""
    value = str(email or "").strip().lower()
    if "@" in value:
        return value.split("@", 1)[1]
    return ""


def extract_email_local(email: Any) -> str:
    """Extract local part from email address."""
    value = str(email or "").strip().lower()
    if "@" in value:
        return value.split("@", 1)[0]
    return value
