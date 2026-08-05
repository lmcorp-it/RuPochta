"""Centralized API error handling.

Provides consistent HTTP error responses across all API endpoints.
Centralizes error messages and status codes for maintainability.
"""

from fastapi import HTTPException
from typing import Optional


class APIError:
    """Centralized API error definitions and factory methods.
    
    All methods return HTTPException instances ready to raise in handlers.
    This ensures consistent error responses across the API.
    """
    
    @staticmethod
    def unauthorized(detail: str = "Unauthorized") -> HTTPException:
        """401 Unauthorized error.
        
        Args:
            detail: Error message detail
        
        Returns:
            HTTPException with status 401
        """
        return HTTPException(status_code=401, detail=detail)
    
    @staticmethod
    def forbidden(detail: str = "Access denied") -> HTTPException:
        """403 Forbidden error.
        
        Args:
            detail: Error message detail
        
        Returns:
            HTTPException with status 403
        """
        return HTTPException(status_code=403, detail=detail)
    
    @staticmethod
    def bad_request(detail: str = "Invalid request") -> HTTPException:
        """400 Bad Request error.
        
        Args:
            detail: Error message detail
        
        Returns:
            HTTPException with status 400
        """
        return HTTPException(status_code=400, detail=detail)
    
    @staticmethod
    def not_found(resource: str) -> HTTPException:
        """404 Not Found error.
        
        Args:
            resource: Name of the resource that was not found
        
        Returns:
            HTTPException with status 404
        """
        return HTTPException(status_code=404, detail=f"{resource} not found")
    
    @staticmethod
    def server_error(detail: str = "Internal server error") -> HTTPException:
        """500 Internal Server Error.
        
        Args:
            detail: Error message detail
        
        Returns:
            HTTPException with status 500
        """
        return HTTPException(status_code=500, detail=detail)
    
    @staticmethod
    def invalid_domain(domain: str) -> HTTPException:
        """400 error for invalid domain.
        
        Args:
            domain: The domain that was invalid
        
        Returns:
            HTTPException with status 400
        """
        return HTTPException(status_code=400, detail=f"Invalid domain: {domain}")
    
    @staticmethod
    def invalid_email(email: str) -> HTTPException:
        """400 error for invalid email.
        
        Args:
            email: The email that was invalid
        
        Returns:
            HTTPException with status 400
        """
        return HTTPException(status_code=400, detail=f"Invalid email: {email}")
    
    @staticmethod
    def invalid_credentials() -> HTTPException:
        """401 error for invalid credentials.
        
        Returns:
            HTTPException with status 401
        """
        return HTTPException(status_code=401, detail="Invalid credentials")
    
    @staticmethod
    def missing_parameter(param_name: str) -> HTTPException:
        """400 error for missing required parameter.
        
        Args:
            param_name: Name of the missing parameter
        
        Returns:
            HTTPException with status 400
        """
        return HTTPException(status_code=400, detail=f"Missing required parameter: {param_name}")
    
    @staticmethod
    def invalid_json() -> HTTPException:
        """400 error for invalid JSON in request body.
        
        Returns:
            HTTPException with status 400
        """
        return HTTPException(status_code=400, detail="Invalid JSON in request body")
    
    @staticmethod
    def quota_exceeded() -> HTTPException:
        """429 error for quota/rate limit exceeded.
        
        Returns:
            HTTPException with status 429
        """
        return HTTPException(status_code=429, detail="Request rate limit exceeded")
    
    @staticmethod
    def conflict(detail: str = "Resource already exists") -> HTTPException:
        """409 Conflict error.
        
        Args:
            detail: Error message detail
        
        Returns:
            HTTPException with status 409
        """
        return HTTPException(status_code=409, detail=detail)
