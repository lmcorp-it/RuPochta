"""Tests for Phase 2 utility modules.

Tests cover database operations, mail protocol, and authentication utilities.
"""

import json
import sqlite3
import pytest
from unittest.mock import Mock, patch, MagicMock
from utils.database import DBHelper
from utils.mail_protocol import IMAPHelper, SMTPHelper
from utils.authentication import AuthGuard
from fastapi import HTTPException


# ============================================================================
# Tests for utils.database
# ============================================================================

class TestDBHelper:
    """Tests for database utilities."""
    
    @staticmethod
    def create_mock_cursor(columns, data):
        """Create a mock cursor with description and data."""
        cursor = Mock()
        cursor.description = [(col,) for col in columns]
        cursor.fetchone = Mock(return_value=data if data else None)
        cursor.fetchall = Mock(return_value=data if data else [])
        return cursor
    
    def test_row_to_dict_basic(self):
        """Test converting row to dictionary."""
        cursor = self.create_mock_cursor(["id", "name"], (1, "John"))
        cursor.fetchone = Mock(return_value=(1, "John"))
        result = DBHelper.row_to_dict(cursor, (1, "John"))
        assert result == {"id": 1, "name": "John"}
    
    def test_row_to_dict_none(self):
        """Test row_to_dict with None row."""
        cursor = self.create_mock_cursor(["id", "name"], None)
        result = DBHelper.row_to_dict(cursor, None)
        assert result is None
    
    def test_rows_to_dicts_basic(self):
        """Test converting multiple rows to dictionaries."""
        cursor = self.create_mock_cursor(["id", "name"], None)
        rows = [(1, "John"), (2, "Jane")]
        result = DBHelper.rows_to_dicts(cursor, rows)
        assert result == [{"id": 1, "name": "John"}, {"id": 2, "name": "Jane"}]
    
    def test_rows_to_dicts_empty(self):
        """Test rows_to_dicts with empty list."""
        cursor = self.create_mock_cursor(["id", "name"], None)
        result = DBHelper.rows_to_dicts(cursor, [])
        assert result == []
    
    def test_parse_json_field_valid(self):
        """Test parsing valid JSON field."""
        row = {"id": 1, "metadata": '{"key": "value"}'}
        result = DBHelper.parse_json_field(row, "metadata")
        assert result["metadata"] == {"key": "value"}
    
    def test_parse_json_field_invalid(self):
        """Test parsing invalid JSON field."""
        row = {"id": 1, "metadata": "not json"}
        result = DBHelper.parse_json_field(row, "metadata")
        assert result["metadata"] == {}
    
    def test_parse_json_field_empty(self):
        """Test parsing empty JSON field."""
        row = {"id": 1, "metadata": ""}
        result = DBHelper.parse_json_field(row, "metadata")
        assert result["metadata"] == ""
    
    def test_parse_json_fields_multiple(self):
        """Test parsing multiple JSON fields."""
        row = {"id": 1, "meta": '{"a": 1}', "tags": '["x", "y"]'}
        result = DBHelper.parse_json_fields(row, ["meta", "tags"])
        assert result["meta"] == {"a": 1}
        assert result["tags"] == ["x", "y"]
    
    def test_cursor_row_dict_basic(self):
        """Test cursor_row_dict convenience method."""
        cursor = self.create_mock_cursor(["id", "name"], None)
        result = DBHelper.cursor_row_dict(cursor, (1, "John"))
        assert result == {"id": 1, "name": "John"}
    
    def test_cursor_row_dict_with_json(self):
        """Test cursor_row_dict with JSON field parsing."""
        cursor = self.create_mock_cursor(["id", "metadata"], None)
        result = DBHelper.cursor_row_dict(cursor, (1, '{"key": "value"}'), json_fields=["metadata"])
        assert result["id"] == 1
        assert result["metadata"] == {"key": "value"}
    
    def test_cursor_row_dict_none(self):
        """Test cursor_row_dict with None row."""
        cursor = self.create_mock_cursor(["id", "name"], None)
        result = DBHelper.cursor_row_dict(cursor, None)
        assert result is None


# ============================================================================
# Tests for utils.mail_protocol
# ============================================================================

class TestIMAPHelper:
    """Tests for IMAP utilities."""
    
    def test_init(self):
        """Test IMAPHelper initialization."""
        helper = IMAPHelper("mail.example.com", 993)
        assert helper.host == "mail.example.com"
        assert helper.port == 993
    
    def test_init_default_port(self):
        """Test IMAPHelper with default port."""
        helper = IMAPHelper("mail.example.com")
        assert helper.port == 993
    
    @patch("imaplib.IMAP4_SSL")
    def test_connect_success(self, mock_imap):
        """Test successful IMAP connection."""
        mock_instance = MagicMock()
        mock_imap.return_value = mock_instance
        
        helper = IMAPHelper("mail.example.com")
        with helper.connect("user@example.com", "password") as imap:
            assert imap == mock_instance
            mock_instance.login.assert_called_once_with("user@example.com", "password")
        
        mock_instance.logout.assert_called_once()
    
    @patch("imaplib.IMAP4_SSL")
    def test_connect_auth_error(self, mock_imap):
        """Test IMAP connection with authentication error."""
        mock_instance = MagicMock()
        mock_instance.login.side_effect = imaplib.IMAP4.error("Authentication failed")
        mock_imap.return_value = mock_instance
        
        helper = IMAPHelper("mail.example.com")
        with pytest.raises(HTTPException) as exc_info:
            with helper.connect("user@example.com", "wrong_password") as imap:
                pass
        
        assert exc_info.value.status_code == 401


class TestSMTPHelper:
    """Tests for SMTP utilities."""
    
    def test_init(self):
        """Test SMTPHelper initialization."""
        helper = SMTPHelper("smtp.example.com", 465)
        assert helper.host == "smtp.example.com"
        assert helper.port == 465
    
    def test_init_default_port(self):
        """Test SMTPHelper with default port."""
        helper = SMTPHelper("smtp.example.com")
        assert helper.port == 465
    
    @patch("smtplib.SMTP_SSL")
    def test_connect_success(self, mock_smtp):
        """Test successful SMTP connection."""
        mock_instance = MagicMock()
        mock_smtp.return_value = mock_instance
        
        helper = SMTPHelper("smtp.example.com")
        with helper.connect("user@example.com", "password") as smtp:
            assert smtp == mock_instance
            mock_instance.login.assert_called_once_with("user@example.com", "password")
        
        mock_instance.quit.assert_called_once()
    
    @patch("smtplib.SMTP_SSL")
    def test_connect_auth_error(self, mock_smtp):
        """Test SMTP connection with authentication error."""
        mock_instance = MagicMock()
        mock_instance.login.side_effect = smtplib.SMTPAuthenticationError(235, "Authentication failed")
        mock_smtp.return_value = mock_instance
        
        helper = SMTPHelper("smtp.example.com")
        with pytest.raises(HTTPException) as exc_info:
            with helper.connect("user@example.com", "wrong_password") as smtp:
                pass
        
        assert exc_info.value.status_code == 401


# ============================================================================
# Tests for utils.authentication
# ============================================================================

class TestAuthGuard:
    """Tests for authentication utilities."""
    
    def test_check_domain_access_same_domain(self):
        """Test domain access check with same domain."""
        AuthGuard.check_domain_access("example.com", "example.com")  # Should not raise
    
    def test_check_domain_access_case_insensitive(self):
        """Test domain access check is case insensitive."""
        AuthGuard.check_domain_access("EXAMPLE.COM", "example.com")  # Should not raise
    
    def test_check_domain_access_different_domain(self):
        """Test domain access check blocks different domain."""
        with pytest.raises(HTTPException) as exc_info:
            AuthGuard.check_domain_access("example.com", "other.com")
        assert exc_info.value.status_code == 403
    
    def test_check_session_valid_ok(self):
        """Test valid session check."""
        session = {"user_id": "alice"}
        AuthGuard.check_session_valid(session, "alice")  # Should not raise
    
    def test_check_session_valid_no_session(self):
        """Test session check with no session."""
        with pytest.raises(HTTPException) as exc_info:
            AuthGuard.check_session_valid(None, "alice")
        assert exc_info.value.status_code == 401
        assert "No active session" in exc_info.value.detail
    
    def test_check_session_valid_user_mismatch(self):
        """Test session check with mismatched user."""
        session = {"user_id": "alice"}
        with pytest.raises(HTTPException) as exc_info:
            AuthGuard.check_session_valid(session, "bob")
        assert exc_info.value.status_code == 401
        assert "Session user mismatch" in exc_info.value.detail
    
    def test_check_admin_ok(self):
        """Test admin check passes for admin user."""
        user = {"user_id": "alice", "is_admin": True}
        AuthGuard.check_admin(user)  # Should not raise
    
    def test_check_admin_not_admin(self):
        """Test admin check blocks non-admin user."""
        user = {"user_id": "alice", "is_admin": False}
        with pytest.raises(HTTPException) as exc_info:
            AuthGuard.check_admin(user)
        assert exc_info.value.status_code == 403
    
    def test_check_admin_no_data(self):
        """Test admin check with no user data."""
        with pytest.raises(HTTPException) as exc_info:
            AuthGuard.check_admin(None)
        assert exc_info.value.status_code == 403
    
    def test_check_user_exists_ok(self):
        """Test user exists check passes."""
        user = {"user_id": "alice"}
        AuthGuard.check_user_exists(user)  # Should not raise
    
    def test_check_user_exists_missing(self):
        """Test user exists check fails for missing user."""
        with pytest.raises(HTTPException) as exc_info:
            AuthGuard.check_user_exists(None)
        assert exc_info.value.status_code == 404
    
    def test_check_user_active_ok(self):
        """Test user active check passes."""
        user = {"user_id": "alice", "is_active": True}
        AuthGuard.check_user_active(user)  # Should not raise
    
    def test_check_user_active_inactive(self):
        """Test user active check fails for inactive user."""
        user = {"user_id": "alice", "is_active": False}
        with pytest.raises(HTTPException) as exc_info:
            AuthGuard.check_user_active(user)
        assert exc_info.value.status_code == 403
    
    def test_check_has_permission_ok(self):
        """Test permission check passes."""
        user = {"permissions": ["read_mail", "send_mail"]}
        AuthGuard.check_has_permission(user, "read_mail")  # Should not raise
    
    def test_check_has_permission_denied(self):
        """Test permission check fails for missing permission."""
        user = {"permissions": ["read_mail"]}
        with pytest.raises(HTTPException) as exc_info:
            AuthGuard.check_has_permission(user, "admin")
        assert exc_info.value.status_code == 403
    
    def test_get_user_id_from_token_ok(self):
        """Test token parsing extracts user ID."""
        # Placeholder implementation test
        user_id = AuthGuard.get_user_id_from_token("alice_token_xyz")
        assert user_id == "alice_token_xyz"
    
    def test_get_user_id_from_token_missing(self):
        """Test token parsing fails without token."""
        with pytest.raises(HTTPException) as exc_info:
            AuthGuard.get_user_id_from_token(None)
        assert exc_info.value.status_code == 401
