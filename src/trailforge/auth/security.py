"""Password hashing (stdlib hashlib.scrypt) and HMAC-SHA256 signed session tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Final

from trailforge.config import Settings, get_settings

logger = logging.getLogger(__name__)

_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32

DEV_DEFAULT_AUTH_SECRET: Final[str] = "trailforge-dev-secret-change-in-production"
DEV_DEFAULT_PRIVACY_SECRET: Final[str] = "trailforge-dev-privacy-secret-change-in-production"
MIN_PRODUCTION_SECRET_LENGTH: Final[int] = 32


def validate_production_secrets(settings: Settings | None = None) -> None:
    """Fail closed in production when auth or privacy secrets use dev defaults or <32 chars.

    In local development mode, logs a clear warning instead of raising so local Compose
    and offline tests continue to work smoothly.
    """
    cfg = settings or get_settings()
    is_prod = bool(
        getattr(cfg, "is_production_mode", False)
        or getattr(cfg, "auth_cookie_secure", False)
        or bool((os.environ.get("RAILWAY_ENVIRONMENT") or "").strip())
        or getattr(cfg, "registration_mode", "open") != "open"
    )

    auth_key = str(getattr(cfg, "auth_secret_key", "") or "").strip()
    privacy_key = str(getattr(cfg, "privacy_secret_key", "") or "").strip()

    problems: list[str] = []
    if auth_key == DEV_DEFAULT_AUTH_SECRET or len(auth_key) < MIN_PRODUCTION_SECRET_LENGTH:
        problems.append(
            "AUTH_SECRET_KEY must be set to a unique secret of at least "
            f"{MIN_PRODUCTION_SECRET_LENGTH} characters (not the development default)"
        )
    if (
        privacy_key == DEV_DEFAULT_PRIVACY_SECRET
        or len(privacy_key) < MIN_PRODUCTION_SECRET_LENGTH
    ):
        problems.append(
            "TRAILFORGE_PRIVACY_SECRET must be set to a unique secret of at least "
            f"{MIN_PRODUCTION_SECRET_LENGTH} characters (not the development default)"
        )

    if not problems:
        return

    detail = "; ".join(problems)
    if is_prod:
        raise RuntimeError(f"Insecure production secret configuration: {detail}")

    logger.warning(
        "TrailForge is running with insecure development secrets (%s). "
        "Set strong 32+ character secrets before deploying publicly.",
        detail,
    )


def normalize_email(email: str) -> str:
    """Normalize an email address for case-insensitive unique storage."""
    cleaned = email.strip().lower()
    if "@" not in cleaned or cleaned.startswith("@") or cleaned.endswith("@"):
        raise ValueError("Invalid email address")
    local, _, domain = cleaned.partition("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("Invalid email address")
    return cleaned


def hash_password(password: str) -> str:
    """Hash a plaintext password using stdlib scrypt with a 16-byte random salt."""
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters long")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a plaintext password against a stored scrypt hash in constant time."""
    parts = stored_hash.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except ValueError:
        return False
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=len(expected),
    )
    return hmac.compare_digest(derived, expected)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(token_part: str) -> bytes:
    padding = "=" * (-len(token_part) % 4)
    return base64.urlsafe_b64decode(token_part + padding)


def create_access_token(
    user_id: str,
    email: str,
    *,
    token_version: int = 1,
    settings: Settings | None = None,
    ttl_seconds: int | None = None,
) -> str:
    """Create a compact HMAC-SHA256 signed session token (`payload_b64.sig_b64`)."""
    cfg = settings or get_settings()
    now = int(time.time())
    ttl = ttl_seconds if ttl_seconds is not None else cfg.auth_token_ttl_seconds
    payload = {
        "sub": str(user_id),
        "email": email,
        "tv": int(token_version),
        "iat": now,
        "exp": now + ttl,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64url_encode(payload_bytes)
    sig = hmac.new(
        cfg.auth_secret_key.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload_b64}.{_b64url_encode(sig)}"


def verify_access_token(
    token: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """Validate signature and expiry of a session token; returns payload or None."""
    if not token or "." not in token:
        return None
    parts = token.split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig_b64 = parts
    cfg = settings or get_settings()
    expected_sig = hmac.new(
        cfg.auth_secret_key.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        actual_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(actual_sig, expected_sig):
            return None
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    sub = payload.get("sub")
    if isinstance(exp, bool) or not isinstance(exp, int) or not isinstance(sub, str):
        return None
    if "tv" in payload:
        tv = payload["tv"]
        if isinstance(tv, bool) or not isinstance(tv, int):
            return None
    if int(time.time()) >= exp:
        return None
    return payload
