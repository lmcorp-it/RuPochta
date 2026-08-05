"""Tests for Phase 1 utility modules.

Tests cover configuration, normalization, errors, and validation utilities.
"""

import os
import pytest
from utils.config import (
    get_env_str, get_env_lower, get_env_bool, get_env_int,
    get_env_list, get_env_fallback
)
from utils.normalization import (
    normalize_email, normalize_domain, normalize_login, normalize_string
)
from utils.errors import APIError
from utils.validation import InputValidator, ValidationError


# ============================================================================
# Tests for utils.config
# ============================================================================

class TestConfig:
    """Tests for configuration utilities."""
    
    def test_get_env_str_basic(self):
        """Test basic environment variable retrieval."""
        os.environ["TEST_VAR"] = "  hello  "
        assert get_env_str("TEST_VAR") == "hello"
    
    def test_get_env_str_default(self):
        """Test default value when variable not set."""
        if "NONEXISTENT_VAR" in os.environ:
            del os.environ["NONEXISTENT_VAR"]
        assert get_env_str("NONEXISTENT_VAR") == ""
        assert get_env_str("NONEXISTENT_VAR", "default") == "default"
    
    def test_get_env_lower(self):
        """Test lowercase environment variable."""
        os.environ["TEST_LOWER"] = "  HELLO  "
        assert get_env_lower("TEST_LOWER") == "hello"
    
    def test_get_env_bool_true(self):
        """Test boolean parsing for true values."""
        for val in ["true", "True", "TRUE", "yes", "1", "on"]:
            os.environ["TEST_BOOL"] = val
            assert get_env_bool("TEST_BOOL") is True
    
    def test_get_env_bool_false(self):
        """Test boolean parsing for false values."""
        for val in ["false", "False", "no", "0", "off", ""]:
            os.environ["TEST_BOOL"] = val
            assert get_env_bool("TEST_BOOL") is False
    
    def test_get_env_bool_default(self):
        """Test boolean default value."""
        if "NONEXISTENT_BOOL" in os.environ:
            del os.environ["NONEXISTENT_BOOL"]
        assert get_env_bool("NONEXISTENT_BOOL") is False
        assert get_env_bool("NONEXISTENT_BOOL", True) is True
    
    def test_get_env_int(self):
        """Test integer environment variable."""
        os.environ["TEST_INT"] = "  42  "
        assert get_env_int("TEST_INT") == 42
    
    def test_get_env_int_invalid(self):
        """Test integer parsing with invalid value."""
        os.environ["TEST_INT_BAD"] = "not_a_number"
        assert get_env_int("TEST_INT_BAD") == 0
        assert get_env_int("TEST_INT_BAD", 99) == 99
    
    def test_get_env_list(self):
        """Test list parsing from comma-separated values."""
        os.environ["TEST_LIST"] = "a, b, c"
        assert get_env_list("TEST_LIST") == ["a", "b", "c"]
    
    def test_get_env_list_custom_separator(self):
        """Test list parsing with custom separator."""
        os.environ["TEST_LIST"] = "a;b;c"
        assert get_env_list("TEST_LIST", separator=";") == ["a", "b", "c"]
    
    def test_get_env_list_empty(self):
        """Test list parsing with empty value."""
        os.environ["TEST_LIST_EMPTY"] = ""
        assert get_env_list("TEST_LIST_EMPTY") == []
    
    def test_get_env_fallback(self):
        """Test fallback to first non-empty variable."""
        if "FALLBACK_1" in os.environ:
            del os.environ["FALLBACK_1"]
        os.environ["FALLBACK_2"] = "value2"
        os.environ["FALLBACK_3"] = "value3"
        assert get_env_fallback("FALLBACK_1", "FALLBACK_2", "FALLBACK_3") == "value2"


# ============================================================================
# Tests for utils.normalization
# ============================================================================

class TestNormalization:
    """Tests for string normalization utilities."""
    
    def test_normalize_email(self):
        """Test email normalization."""
        assert normalize_email("  User@Example.COM  ") == "user@example.com"
        assert normalize_email("admin@domain.org") == "admin@domain.org"
    
    def test_normalize_email_empty(self):
        """Test email normalization with empty/None values."""
        assert normalize_email("") == ""
        assert normalize_email(None) == ""
        assert normalize_email("   ") == ""
    
    def test_normalize_domain(self):
        """Test domain normalization."""
        assert normalize_domain("  Example.COM.  ") == "example.com."
        assert normalize_domain("DOMAIN.ORG") == "domain.org"
    
    def test_normalize_domain_empty(self):
        """Test domain normalization with empty/None values."""
        assert normalize_domain("") == ""
        assert normalize_domain(None) == ""
    
    def test_normalize_login(self):
        """Test login normalization."""
        assert normalize_login("  Admin  ") == "admin"
        assert normalize_login("USER_NAME") == "user_name"
    
    def test_normalize_string(self):
        """Test generic string normalization."""
        assert normalize_string("  HELLO  ") == "hello"
        assert normalize_string("  HELLO  ", lowercase=False) == "hello"


# ============================================================================
# Tests for utils.errors
# ============================================================================

class TestAPIError:
    """Tests for API error factory methods."""
    
    def test_unauthorized(self):
        """Test 401 Unauthorized error."""
        error = APIError.unauthorized()
        assert error.status_code == 401
        assert error.detail == "Unauthorized"
    
    def test_forbidden(self):
        """Test 403 Forbidden error."""
        error = APIError.forbidden()
        assert error.status_code == 403
        assert error.detail == "Access denied"
    
    def test_bad_request(self):
        """Test 400 Bad Request error."""
        error = APIError.bad_request("Custom message")
        assert error.status_code == 400
        assert error.detail == "Custom message"
    
    def test_not_found(self):
        """Test 404 Not Found error."""
        error = APIError.not_found("User")
        assert error.status_code == 404
        assert error.detail == "User not found"
    
    def test_server_error(self):
        """Test 500 Server error."""
        error = APIError.server_error()
        assert error.status_code == 500
    
    def test_invalid_email(self):
        """Test invalid email error."""
        error = APIError.invalid_email("bad@email")
        assert error.status_code == 400
        assert "bad@email" in error.detail
    
    def test_invalid_credentials(self):
        """Test invalid credentials error."""
        error = APIError.invalid_credentials()
        assert error.status_code == 401
        assert "Invalid credentials" in error.detail
    
    def test_quota_exceeded(self):
        """Test rate limit/quota exceeded error."""
        error = APIError.quota_exceeded()
        assert error.status_code == 429


# ============================================================================
# Tests for utils.validation
# ============================================================================

class TestInputValidator:
    """Tests for input validation utilities."""
    
    def test_validate_email_valid(self):
        """Test valid email validation."""
        assert InputValidator.validate_email("user@example.com") == "user@example.com"
        assert InputValidator.validate_email("User@Example.COM") == "user@example.com"
    
    def test_validate_email_invalid(self):
        """Test invalid email detection."""
        with pytest.raises(ValidationError):
            InputValidator.validate_email("invalid_email")
        with pytest.raises(ValidationError):
            InputValidator.validate_email("@example.com")
        with pytest.raises(ValidationError):
            InputValidator.validate_email("")
    
    def test_validate_domain_valid(self):
        """Test valid domain validation."""
        assert InputValidator.validate_domain("example.com") == "example.com"
        assert InputValidator.validate_domain("DOMAIN.ORG") == "domain.org"
    
    def test_validate_domain_invalid(self):
        """Test invalid domain detection."""
        with pytest.raises(ValidationError):
            InputValidator.validate_domain("invalid")
        with pytest.raises(ValidationError):
            InputValidator.validate_domain(".com")
    
    def test_validate_ip_not_loopback_valid(self):
        """Test valid IP validation."""
        assert InputValidator.validate_ip_not_loopback("192.168.1.1") == "192.168.1.1"
        assert InputValidator.validate_ip_not_loopback("8.8.8.8") == "8.8.8.8"
    
    def test_validate_ip_not_loopback_invalid(self):
        """Test loopback IP detection."""
        with pytest.raises(ValidationError, match="Loopback"):
            InputValidator.validate_ip_not_loopback("127.0.0.1")
        with pytest.raises(ValidationError, match="Loopback"):
            InputValidator.validate_ip_not_loopback("::1")
    
    def test_parse_bool(self):
        """Test boolean string parsing."""
        assert InputValidator.parse_bool("true") is True
        assert InputValidator.parse_bool("yes") is True
        assert InputValidator.parse_bool("1") is True
        assert InputValidator.parse_bool("false") is False
        assert InputValidator.parse_bool("no") is False
        assert InputValidator.parse_bool("") is False
    
    def test_is_valid_username(self):
        """Test username validation."""
        assert InputValidator.is_valid_username("user_123") is True
        assert InputValidator.is_valid_username("admin-user") is True
        assert InputValidator.is_valid_username("user@example") is False
        assert InputValidator.is_valid_username("") is False
    
    def test_validate_username(self):
        """Test username validation with exception."""
        assert InputValidator.validate_username("valid_user") == "valid_user"
        with pytest.raises(ValidationError):
            InputValidator.validate_username("invalid@user")
    
    def test_validate_nonempty(self):
        """Test non-empty string validation."""
        assert InputValidator.validate_nonempty("hello") == "hello"
        assert InputValidator.validate_nonempty("  hello  ") == "hello"
        with pytest.raises(ValidationError, match="cannot be empty"):
            InputValidator.validate_nonempty("")
        with pytest.raises(ValidationError, match="cannot be empty"):
            InputValidator.validate_nonempty("   ")
