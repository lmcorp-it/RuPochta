"""String normalization utilities for consistent data handling.

Provides functions for normalizing email addresses, domain names,
and general strings throughout the application.
"""

from typing import Optional


def normalize_email(email: Optional[str]) -> str:
    """Normalize email: strip whitespace and convert to lowercase.
    
    Args:
        email: Email string to normalize (can be None or empty)
    
    Returns:
        Normalized email string (lowercase, whitespace stripped)
    
    Example:
        >>> normalize_email("  User@Example.COM  ")
        'user@example.com'
    """
    if not email:
        return ""
    return str(email).strip().lower()


def normalize_domain(domain: Optional[str]) -> str:
    """Normalize domain: strip whitespace and convert to lowercase.
    
    Args:
        domain: Domain string to normalize (can be None or empty)
    
    Returns:
        Normalized domain string (lowercase, whitespace stripped)
    
    Example:
        >>> normalize_domain("  Example.COM.  ")
        'example.com.'
    """
    if not domain:
        return ""
    return str(domain).strip().lower()


def normalize_login(login: Optional[str]) -> str:
    """Normalize login name: strip and lowercase.
    
    Args:
        login: Login string to normalize (can be None or empty)
    
    Returns:
        Normalized login string (lowercase, whitespace stripped)
    
    Example:
        >>> normalize_login("  Admin  ")
        'admin'
    """
    if not login:
        return ""
    return str(login).strip().lower()


def normalize_string(value: Optional[str], *, lowercase: bool = True) -> str:
    """Generic string normalization utility.
    
    Args:
        value: String to normalize (can be None or empty)
        lowercase: Whether to convert to lowercase (default: True)
    
    Returns:
        Normalized string
    
    Example:
        >>> normalize_string("  HELLO  ")
        'hello'
        >>> normalize_string("  HELLO  ", lowercase=False)
        'hello'
    """
    if not value:
        return ""
    result = str(value).strip()
    return result.lower() if lowercase else result
