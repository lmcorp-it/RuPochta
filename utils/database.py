"""Database utility functions for SQLite operations.

Provides helpers for row-to-dict conversion, JSON parsing,
and common database operations throughout the application.
"""

import json
import sqlite3
from typing import Any, Dict, List, Optional


class DBHelper:
    """Database utility functions for SQLite operations.
    
    Centralizes common database patterns like row-to-dict conversion
    and JSON field parsing to ensure consistency across the app.
    """
    
    @staticmethod
    def row_to_dict(cursor: sqlite3.Cursor, row: tuple) -> Optional[Dict[str, Any]]:
        """Convert a single cursor row to dictionary.
        
        Args:
            cursor: SQLite cursor with description set
            row: Tuple from cursor.fetchone() or similar
        
        Returns:
            Dictionary mapping column names to values, or None if row is None
        
        Example:
            >>> cursor = conn.cursor()
            >>> cursor.execute("SELECT id, name FROM users WHERE id = ?", (1,))
            >>> row = cursor.fetchone()
            >>> user = DBHelper.row_to_dict(cursor, row)
            >>> user['name']
            'John Doe'
        """
        if row is None:
            return None
        cols = [col[0] for col in cursor.description]
        return dict(zip(cols, row))
    
    @staticmethod
    def rows_to_dicts(cursor: sqlite3.Cursor, rows: List[tuple]) -> List[Dict[str, Any]]:
        """Convert cursor rows to list of dictionaries.
        
        Args:
            cursor: SQLite cursor with description set
            rows: List of tuples from cursor.fetchall() or similar
        
        Returns:
            List of dictionaries, each mapping column names to values
        
        Example:
            >>> cursor = conn.cursor()
            >>> cursor.execute("SELECT id, name FROM users")
            >>> rows = cursor.fetchall()
            >>> users = DBHelper.rows_to_dicts(cursor, rows)
        """
        if not rows:
            return []
        cols = [col[0] for col in cursor.description]
        return [dict(zip(cols, row)) for row in rows]
    
    @staticmethod
    def parse_json_field(row: Dict[str, Any], field: str) -> Dict[str, Any]:
        """Parse JSON field in a row dictionary.
        
        Args:
            row: Dictionary with potential JSON field
            field: Name of field to parse as JSON
        
        Returns:
            The row with field parsed from JSON string to object
        
        Example:
            >>> row = {'id': 1, 'metadata': '{"key": "value"}'}
            >>> row = DBHelper.parse_json_field(row, 'metadata')
            >>> row['metadata']['key']
            'value'
        """
        if row and row.get(field):
            try:
                row[field] = json.loads(row[field])
            except (json.JSONDecodeError, TypeError):
                row[field] = {}
        return row
    
    @staticmethod
    def parse_json_fields(row: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
        """Parse multiple JSON fields in a row dictionary.
        
        Args:
            row: Dictionary with potential JSON fields
            fields: List of field names to parse as JSON
        
        Returns:
            The row with all specified fields parsed from JSON
        
        Example:
            >>> row = {'id': 1, 'meta': '{"a": 1}', 'tags': '["x", "y"]'}
            >>> row = DBHelper.parse_json_fields(row, ['meta', 'tags'])
        """
        for field in fields:
            row = DBHelper.parse_json_field(row, field)
        return row
    
    @staticmethod
    def cursor_row_dict(cursor: sqlite3.Cursor, row: tuple,
                       json_fields: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """Convert row to dict and optionally parse JSON fields.
        
        Convenience method combining row_to_dict and parse_json_fields
        for common use case of fetching a row and parsing JSON columns.
        
        Args:
            cursor: SQLite cursor with description set
            row: Tuple from cursor.fetchone() or similar
            json_fields: Optional list of field names containing JSON to parse
        
        Returns:
            Dictionary mapping column names to values with JSON fields parsed,
            or None if row is None
        
        Example:
            >>> cursor = conn.cursor()
            >>> cursor.execute("SELECT id, name, metadata FROM users WHERE id = ?", (1,))
            >>> row = cursor.fetchone()
            >>> user = DBHelper.cursor_row_dict(cursor, row, json_fields=['metadata'])
            >>> user['metadata']
            {'key': 'value'}
        """
        result = DBHelper.row_to_dict(cursor, row)
        if result and json_fields:
            result = DBHelper.parse_json_fields(result, json_fields)
        return result
    
    @staticmethod
    def cursor_rows_dicts(cursor: sqlite3.Cursor, rows: List[tuple],
                         json_fields: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Convert rows to list of dicts and optionally parse JSON fields.
        
        Convenience method combining rows_to_dicts and parse_json_fields
        for common use case of fetching rows and parsing JSON columns.
        
        Args:
            cursor: SQLite cursor with description set
            rows: List of tuples from cursor.fetchall() or similar
            json_fields: Optional list of field names containing JSON to parse
        
        Returns:
            List of dictionaries with column mappings and JSON fields parsed
        
        Example:
            >>> cursor = conn.cursor()
            >>> cursor.execute("SELECT id, name, metadata FROM users")
            >>> rows = cursor.fetchall()
            >>> users = DBHelper.cursor_rows_dicts(cursor, rows, json_fields=['metadata'])
        """
        results = DBHelper.rows_to_dicts(cursor, rows)
        if results and json_fields:
            results = [DBHelper.parse_json_fields(row, json_fields) for row in results]
        return results
