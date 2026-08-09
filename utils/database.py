"""
SQLite database utilities: row-to-dict conversion, JSON field helpers.
"""

import json
import sqlite3
from typing import Any, Dict, List, Optional


def db_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert sqlite3.Row to dictionary."""
    return dict(row)


def db_parse_json_field(value: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse a JSON field safely; return None on invalid JSON."""
    if not value:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def db_encode_json_field(data: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode data to JSON for storage; return None for empty input."""
    if not data:
        return None
    try:
        return json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def db_rows_to_dicts(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    """Convert list of sqlite3.Row objects to list of dictionaries."""
    return [dict(row) for row in rows]


def db_bool_from_int(value: Optional[int]) -> bool:
    """Convert integer/nullable int column to boolean."""
    return bool(value)


def db_int_from_value(value: Any) -> int:
    """Convert value to integer safely."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def db_str_from_value(value: Any) -> str:
    """Convert value to string safely."""
    if value is None:
        return ""
    return str(value)
