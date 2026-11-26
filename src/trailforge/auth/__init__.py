"""Self-hostable authentication (scrypt password hashing + HMAC-SHA256 signed tokens)."""

from trailforge.auth.dependencies import (
    extract_token,
    get_current_user,
    get_optional_current_user,
)
from trailforge.auth.security import (
    create_access_token,
    hash_password,
    normalize_email,
    verify_access_token,
    verify_password,
)

__all__ = [
    "create_access_token",
    "extract_token",
    "get_current_user",
    "get_optional_current_user",
    "hash_password",
    "normalize_email",
    "verify_access_token",
    "verify_password",
]
