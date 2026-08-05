"""Authentication and authorization utilities.

Provides centralized authentication checks and authorization guards
for consistent access control across the application.
"""

from typing import Dict, Any, Optional
from fastapi import HTTPException
from utils.normalization import normalize_domain


class AuthGuard:
    """Authentication and authorization checking utilities.
    
    Provides type-safe methods for common authentication and
    authorization checks to ensure consistency across handlers.
    """
    
    @staticmethod
    def check_domain_access(user_domain: str, requested_domain: str) -> None:
        """Check if user has access to the requested domain.
        
        Performs normalized domain comparison to ensure user can only
        access domains they are authorized for.
        
        Args:
            user_domain: Domain user is authorized for
            requested_domain: Domain user is requesting access to
        
        Raises:
            HTTPException: 403 if user doesn't have access to domain
        
        Example:
            >>> AuthGuard.check_domain_access("example.com", "example.com")  # OK
            >>> AuthGuard.check_domain_access("example.com", "other.com")    # Raises 403
        """
        if normalize_domain(user_domain) != normalize_domain(requested_domain):
            raise HTTPException(status_code=403, detail="Access denied: domain mismatch")
    
    @staticmethod
    def check_session_valid(session_data: Optional[Dict[str, Any]],
                           expected_user_id: str) -> None:
        """Check if session is valid and matches expected user.
        
        Validates that a session exists and that it belongs to the
        expected user (prevents session hijacking).
        
        Args:
            session_data: Session dictionary from storage
            expected_user_id: User ID the session should belong to
        
        Raises:
            HTTPException: 401 if session is missing or user mismatch
        
        Example:
            >>> session = {"user_id": "alice"}
            >>> AuthGuard.check_session_valid(session, "alice")      # OK
            >>> AuthGuard.check_session_valid(None, "alice")         # Raises 401
            >>> AuthGuard.check_session_valid(session, "bob")        # Raises 401
        """
        if not session_data:
            raise HTTPException(status_code=401, detail="No active session")
        
        if session_data.get("user_id") != expected_user_id:
            raise HTTPException(status_code=401, detail="Session user mismatch")
    
    @staticmethod
    def check_admin(user_data: Optional[Dict[str, Any]]) -> None:
        """Check if user has admin privileges.
        
        Validates that user exists and has admin flag set.
        
        Args:
            user_data: User data dictionary from database
        
        Raises:
            HTTPException: 403 if user is not admin
        
        Example:
            >>> user = {"user_id": "alice", "is_admin": True}
            >>> AuthGuard.check_admin(user)                    # OK
            >>> AuthGuard.check_admin({"is_admin": False})     # Raises 403
        """
        if not user_data or not user_data.get("is_admin"):
            raise HTTPException(status_code=403, detail="Admin access required")
    
    @staticmethod
    def check_user_exists(user_data: Optional[Dict[str, Any]]) -> None:
        """Check if user data exists and is valid.
        
        Validates that user record exists and has required fields.
        
        Args:
            user_data: User data dictionary from database
        
        Raises:
            HTTPException: 404 if user doesn't exist
        
        Example:
            >>> user = {"user_id": "alice"}
            >>> AuthGuard.check_user_exists(user)      # OK
            >>> AuthGuard.check_user_exists(None)      # Raises 404
        """
        if not user_data or not user_data.get("user_id"):
            raise HTTPException(status_code=404, detail="User not found")
    
    @staticmethod
    def check_user_active(user_data: Optional[Dict[str, Any]]) -> None:
        """Check if user account is active.
        
        Validates that user is not disabled or deactivated.
        
        Args:
            user_data: User data dictionary from database
        
        Raises:
            HTTPException: 403 if user is not active
        
        Example:
            >>> user = {"user_id": "alice", "is_active": True}
            >>> AuthGuard.check_user_active(user)              # OK
            >>> AuthGuard.check_user_active({"is_active": False})  # Raises 403
        """
        if not user_data or not user_data.get("is_active"):
            raise HTTPException(status_code=403, detail="User account is not active")
    
    @staticmethod
    def check_has_permission(user_data: Optional[Dict[str, Any]], permission: str) -> None:
        """Check if user has specific permission.
        
        Validates that user has required permission.
        
        Args:
            user_data: User data dictionary from database
            permission: Permission string to check
        
        Raises:
            HTTPException: 403 if user lacks permission
        
        Example:
            >>> user = {"permissions": ["read_mail", "send_mail"]}
            >>> AuthGuard.check_has_permission(user, "read_mail")   # OK
            >>> AuthGuard.check_has_permission(user, "admin")       # Raises 403
        """
        if not user_data:
            raise HTTPException(status_code=403, detail="Access denied")
        
        permissions = user_data.get("permissions", [])
        if permission not in permissions:
            raise HTTPException(status_code=403, detail=f"Permission denied: {permission}")
    
    @staticmethod
    def get_user_id_from_token(token: Optional[str]) -> str:
        """Extract user ID from authentication token.
        
        Parses token to get user ID. Raises error if token is invalid.
        
        Args:
            token: Authentication token
        
        Returns:
            User ID extracted from token
        
        Raises:
            HTTPException: 401 if token is missing or invalid
        
        Note:
            This is a placeholder implementation. Actual token validation
            should check cryptographic signatures and expiration.
        
        Example:
            >>> user_id = AuthGuard.get_user_id_from_token("token_xyz")
        """
        if not token:
            raise HTTPException(status_code=401, detail="Missing authentication token")
        
        # Placeholder: actual implementation should parse JWT or similar
        # For now, assume token format is user_id or includes user_id
        try:
            # This is a simplified placeholder - real implementation would
            # validate JWT signature, check expiration, etc.
            return token
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid authentication token")
