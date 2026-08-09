"""
Environment variable parsing and configuration helpers.
"""

import os
from typing import Any, List, Optional, Dict


def env_string(key: str, default: str = "") -> str:
    """Get environment variable as string, stripped."""
    return os.environ.get(key, default).strip()


def env_string_or_fallback(key: str, fallback_key: str, default: str = "") -> str:
    """Get environment variable, with fallback to another key."""
    value = os.environ.get(key, "").strip()
    if value:
        return value
    return os.environ.get(fallback_key, default).strip()


def env_int(key: str, default: int = 0) -> int:
    """Get environment variable as integer."""
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def env_float(key: str, default: float = 0.0) -> float:
    """Get environment variable as float."""
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def env_bool(key: str, default: bool = False) -> bool:
    """Get environment variable as boolean.
    
    Considers these values as False: '0', 'false', 'no', 'off'
    Considers these values as True: '1', 'true', 'yes', 'on'
    """
    value = os.environ.get(key, "").strip().lower()
    if not value:
        return default
    return value not in {"0", "false", "no", "off"}


def env_list(key: str, separator: str = ",", default: Optional[List[str]] = None) -> List[str]:
    """Get environment variable as list of strings, stripped."""
    if default is None:
        default = []
    value = os.environ.get(key, "")
    if not value:
        return default
    return [item.strip() for item in value.split(separator) if item.strip()]


def env_dict_json(key: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Get environment variable as JSON dictionary."""
    import json
    if default is None:
        default = {}
    value = os.environ.get(key, "").strip()
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default
