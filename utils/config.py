"""Configuration parsing utilities for environment variables.

Provides type-safe helpers for reading environment variables with
automatic stripping, type conversion, and default value handling.
"""

import os
from typing import Optional, List


def get_env_str(key: str, default: str = "") -> str:
    """Get environment variable as stripped string.
    
    Args:
        key: Environment variable name
        default: Default value if not set
    
    Returns:
        Value from environment variable, stripped of whitespace
    """
    return os.environ.get(key, default).strip()


def get_env_lower(key: str, default: str = "") -> str:
    """Get environment variable as lowercase stripped string.
    
    Args:
        key: Environment variable name
        default: Default value if not set
    
    Returns:
        Value from environment variable, lowercase and stripped
    """
    return os.environ.get(key, default).strip().lower()


def get_env_bool(key: str, default: bool = False) -> bool:
    """Get environment variable as boolean.
    
    Accepts: true, yes, 1, on (case-insensitive, with strip)
    
    Args:
        key: Environment variable name
        default: Default value if not set
    
    Returns:
        Boolean value
    """
    value = os.environ.get(key, "").strip().lower()
    if value in ("true", "yes", "1", "on"):
        return True
    if value in ("false", "no", "0", "off", ""):
        return False
    return default


def get_env_int(key: str, default: int = 0) -> int:
    """Get environment variable as integer.
    
    Args:
        key: Environment variable name
        default: Default value if not set or invalid
    
    Returns:
        Integer value
    """
    try:
        return int(os.environ.get(key, str(default)).strip())
    except ValueError:
        return default


def get_env_list(key: str, separator: str = ",", default: Optional[List[str]] = None) -> List[str]:
    """Get environment variable as list (comma-separated by default).
    
    Args:
        key: Environment variable name
        separator: Separator character/string (default: comma)
        default: Default list if not set
    
    Returns:
        List of strings with each item stripped
    """
    value = os.environ.get(key, "").strip()
    if not value:
        return default or []
    return [item.strip() for item in value.split(separator)]


def get_env_fallback(*keys: str, default: str = "") -> str:
    """Get first non-empty environment variable from list of keys.
    
    Args:
        *keys: Environment variable names to try in order
        default: Default value if none are set
    
    Returns:
        Value of first non-empty variable, or default
    """
    for key in keys:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return default
