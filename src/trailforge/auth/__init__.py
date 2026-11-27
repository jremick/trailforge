"""Self-hostable authentication, roles, invite codes, and FastAPI dependencies."""

from trailforge.auth.dependencies import (
    extract_token,
    get_current_user,
    get_optional_current_user,
    require_admin,
    require_user,
)
from trailforge.auth.invites import (
    classify_invite_status,
    create_invite_code,
    generate_invite_code,
    get_demo_banner,
    get_registration_mode,
    normalize_invite_code,
)
from trailforge.auth.security import (
    create_access_token,
    hash_password,
    normalize_email,
    verify_access_token,
    verify_password,
)

__all__ = [
    "classify_invite_status",
    "create_access_token",
    "create_invite_code",
    "extract_token",
    "generate_invite_code",
    "get_current_user",
    "get_demo_banner",
    "get_optional_current_user",
    "get_registration_mode",
    "hash_password",
    "normalize_email",
    "normalize_invite_code",
    "require_admin",
    "require_user",
    "verify_access_token",
    "verify_password",
]
