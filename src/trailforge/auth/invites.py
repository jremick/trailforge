"""Invite code generation, lifecycle helpers, and safe access-control settings lookups."""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from trailforge.config import get_settings
from trailforge.db.models import InviteCode

RegistrationMode = Literal["open", "invite_only", "closed"]
InviteStatus = Literal["active", "exhausted", "expired", "revoked"]

# Human-friendly Crockford-like alphabet omitting 0/O and 1/I to avoid typos
_INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def get_registration_mode() -> RegistrationMode:
    """Safely read registration mode from Settings (or env fallback)."""
    cfg = get_settings()
    val = getattr(cfg, "registration_mode", None)
    if val is not None:
        raw = str(val)
    else:
        raw = os.environ.get("TRAILFORGE_REGISTRATION_MODE") or os.environ.get(
            "REGISTRATION_MODE", "open"
        )
    normalized = raw.strip().lower()
    if normalized in ("open", "invite_only", "closed"):
        return normalized  # type: ignore[return-value]
    return "open"


def get_admin_email() -> str:
    """Safely read bootstrap admin email from Settings (or env fallback)."""
    cfg = get_settings()
    val = getattr(cfg, "admin_email", None)
    if val is not None:
        raw = str(val)
    else:
        raw = os.environ.get("TRAILFORGE_ADMIN_EMAIL") or os.environ.get("ADMIN_EMAIL", "")
    return raw.strip().lower()


def get_demo_banner() -> str:
    """Safely read optional public demo banner text from Settings (or env fallback)."""
    cfg = get_settings()
    val = getattr(cfg, "demo_banner", None)
    if val is not None:
        raw = str(val)
    else:
        raw = os.environ.get("TRAILFORGE_DEMO_BANNER") or os.environ.get("DEMO_BANNER", "")
    return raw.strip()


def normalize_invite_code(code: str) -> str:
    """Trim and uppercase an invite code for case-insensitive matching."""
    return code.strip().upper()


def generate_invite_code() -> str:
    """Generate a crisp ``TF-XXXX-XXXX`` invite code using ``secrets``."""
    part_a = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(4))
    part_b = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(4))
    return f"TF-{part_a}-{part_b}"


def _as_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def classify_invite_status(
    invite: InviteCode,
    *,
    now: datetime | None = None,
) -> InviteStatus:
    """Classify an invite code as active, exhausted, expired, or revoked."""
    current = now or datetime.now(UTC)
    if invite.revoked_at is not None:
        return "revoked"
    if invite.expires_at is not None and _as_utc(invite.expires_at) <= current:
        return "expired"
    if invite.use_count >= invite.max_uses:
        return "exhausted"
    return "active"


def create_invite_code(
    db: Session,
    *,
    label: str | None = None,
    role: str = "member",
    max_uses: int = 1,
    expires_in_days: int | None = None,
    created_by_user_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> InviteCode:
    """Create and flush a new unique ``TF-XXXX-XXXX`` invite code row."""
    clean_role = role.strip().lower()
    if clean_role not in ("member", "admin"):
        raise ValueError("Invite role must be 'member' or 'admin'")
    if max_uses < 1:
        raise ValueError("max_uses must be at least 1")
    if expires_in_days is not None and expires_in_days < 1:
        raise ValueError("expires_in_days must be at least 1")

    current = now or datetime.now(UTC)
    expires_at = current + timedelta(days=expires_in_days) if expires_in_days is not None else None
    clean_label = label.strip()[:120] if label and label.strip() else None

    code: str | None = None
    for _ in range(16):
        candidate = generate_invite_code()
        exists = db.execute(
            select(InviteCode.id).where(InviteCode.code == candidate).limit(1)
        ).scalar_one_or_none()
        if exists is None:
            code = candidate
            break
    if code is None:
        raise RuntimeError("Unable to generate a unique invite code")

    invite = InviteCode(
        code=code,
        label=clean_label,
        role=clean_role,
        max_uses=max_uses,
        use_count=0,
        expires_at=expires_at,
        created_by_user_id=created_by_user_id,
    )
    db.add(invite)
    db.flush()
    return invite
