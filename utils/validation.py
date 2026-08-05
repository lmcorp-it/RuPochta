"""Input validation utilities for consistent data validation.

Provides validators for emails, domains, IPs, and general input
to ensure consistent validation across the application.
"""

import ipaddress
import re
from typing import Optional


class ValidationError(ValueError):
    """Custom validation error for input validation failures."""
    pass


class InputValidator:
    """Input validation utilities.
    
    Provides type-safe validation methods for common input types.
    All validation methods raise ValidationError on failure.
    """
    
    EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
    DOMAIN_REGEX = re.compile(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
    USERNAME_REGEX = re.compile(r"^[a-zA-Z0-9_-]+$")
    
    @staticmethod
    def validate_email(email: str) -> str:
        """Validate email format.
        
        Args:
            email: Email string to validate
        
        Returns:
            The validated email (normalized to lowercase)
        
        Raises:
            ValidationError: If email is invalid
        
        Example:
            >>> InputValidator.validate_email("User@Example.COM")
            'user@example.com'
        """
        if not email or not InputValidator.EMAIL_REGEX.match(email.strip()):
            raise ValidationError(f"Invalid email: {email}")
        return email.strip().lower()
    
    @staticmethod
    def validate_domain(domain: str) -> str:
        """Validate domain format.
        
        Args:
            domain: Domain string to validate
        
        Returns:
            The validated domain (normalized to lowercase)
        
        Raises:
            ValidationError: If domain is invalid
        
        Example:
            >>> InputValidator.validate_domain("Example.COM")
            'example.com'
        """
        if not domain or not InputValidator.DOMAIN_REGEX.match(domain.strip()):
            raise ValidationError(f"Invalid domain: {domain}")
        return domain.strip().lower()
    
    @staticmethod
    def validate_ip_not_loopback(ip_str: str) -> str:
        """Validate IP is not loopback/localhost.
        
        Args:
            ip_str: IP address string to validate
        
        Returns:
            The IP string if valid and not loopback
        
        Raises:
            ValidationError: If IP is invalid or is loopback
        
        Example:
            >>> InputValidator.validate_ip_not_loopback("192.168.1.1")
            '192.168.1.1'
        """
        try:
            ip = ipaddress.ip_address(ip_str.strip())
            if ip.is_loopback:
                raise ValidationError("Loopback addresses not allowed")
            return str(ip)
        except (ipaddress.AddressValueError, ValueError) as e:
            raise ValidationError(f"Invalid IP address: {ip_str}") from e
    
    @staticmethod
    def validate_ip_private(ip_str: str) -> str:
        """Validate IP is in private network range.
        
        Args:
            ip_str: IP address string to validate
        
        Returns:
            The IP string if valid and private
        
        Raises:
            ValidationError: If IP is invalid or not private
        """
        try:
            ip = ipaddress.ip_address(ip_str.strip())
            if not ip.is_private:
                raise ValidationError(f"IP {ip_str} is not in private range")
            return str(ip)
        except (ipaddress.AddressValueError, ValueError) as e:
            raise ValidationError(f"Invalid IP address: {ip_str}") from e
    
    @staticmethod
    def parse_bool(value: str) -> bool:
        """Parse string to boolean.
        
        Accepts: true, yes, 1, on (case-insensitive)
        
        Args:
            value: String value to parse
        
        Returns:
            Boolean value
        
        Example:
            >>> InputValidator.parse_bool("yes")
            True
            >>> InputValidator.parse_bool("no")
            False
        """
        if not value:
            return False
        return value.strip().lower() in ("true", "yes", "1", "on")
    
    @staticmethod
    def is_valid_username(username: str) -> bool:
        """Check if username is valid (alphanumeric, underscore, hyphen).
        
        Args:
            username: Username string to validate
        
        Returns:
            True if username is valid, False otherwise
        
        Example:
            >>> InputValidator.is_valid_username("user_123")
            True
            >>> InputValidator.is_valid_username("user@example")
            False
        """
        if not username:
            return False
        return InputValidator.USERNAME_REGEX.match(username) is not None
    
    @staticmethod
    def validate_username(username: str) -> str:
        """Validate username format.
        
        Args:
            username: Username string to validate
        
        Returns:
            The username if valid
        
        Raises:
            ValidationError: If username is invalid
        """
        if not InputValidator.is_valid_username(username):
            raise ValidationError(f"Invalid username: {username}")
        return username
    
    @staticmethod
    def validate_nonempty(value: str, field_name: str = "Value") -> str:
        """Validate that string is not empty or whitespace-only.
        
        Args:
            value: String to validate
            field_name: Name of field for error message
        
        Returns:
            The trimmed value if valid
        
        Raises:
            ValidationError: If value is empty or whitespace-only
        """
        if not value or not value.strip():
            raise ValidationError(f"{field_name} cannot be empty")
        return value.strip()
