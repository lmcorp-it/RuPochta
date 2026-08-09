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


def is_internal_client_ip(client_ip: str) -> bool:
    """Check if IP is in the internal/private range (127.0.0.1, ::1, 10.0.0.0/8, etc.).
    
    Uses explicit network ranges to match the security-critical requirements:
    - 127.0.0.0/8 (IPv4 loopback)
    - 10.0.0.0/8 (IPv4 private)
    - 172.16.0.0/12 (IPv4 private)
    - 192.168.0.0/16 (IPv4 private)
    - ::1/128 (IPv6 loopback)
    - fc00::/7 (IPv6 unique local)
    - fe80::/10 (IPv6 link-local)
    """
    text = str(client_ip or "").strip()
    if not text:
        return False
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    internal_networks = (
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fc00::/7"),
        ipaddress.ip_network("fe80::/10"),
    )
    return any(addr in network for network in internal_networks)


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
