"""
String normalization utilities for email, domain, and user data.
"""

from typing import Any
import re


def normalize_email(email: Any) -> str:
    """Normalize email address to lowercase, stripped."""
    return str(email or "").strip().lower()


def normalize_domain(domain: Any) -> str:
    """Normalize domain name to lowercase, stripped, with trailing dot removed."""
    return str(domain or "").strip().lower().rstrip(".")


def normalize_string(value: Any) -> str:
    """Normalize string: strip and lowercase."""
    return str(value or "").strip().lower()


def normalize_user(username: Any) -> str:
    """Normalize username to lowercase, stripped."""
    return str(username or "").strip().lower()


def normalize_dn(dn: Any) -> str:
    """Normalize LDAP Distinguished Name: strip without case change."""
    return str(dn or "").strip()


def normalize_dn_casefold(dn: Any) -> str:
    """Normalize LDAP DN to casefolded form for comparison."""
    return normalize_dn(dn).casefold()


def normalize_company_name(company: Any) -> str:
    """Normalize company name by collapsing whitespace and converting to lowercase."""
    clean = re.sub(r"[\s_-]+", " ", str(company or "").strip().lower()).strip()
    return clean


def normalize_multiline_string(value: Any, max_length: int = 300) -> str:
    """Normalize multiline string: collapse newlines, strip, truncate."""
    message = re.sub(r"[\r\n]+", " ", str(value or "")).strip()
    return message[:max_length] if max_length > 0 else message
