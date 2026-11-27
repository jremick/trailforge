"""Private, versioned derivation of community withdrawal identifiers."""

from __future__ import annotations

import hashlib

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import ColumnElement

from trailforge.config import get_settings
from trailforge.db.models import Trip
from trailforge.matching.privacy import compute_contribution_hash


class ContributionKeyUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "A previous community privacy key is unavailable. "
            "Restore it before withdrawing or recomputing contributions."
        )


def contribution_key_id(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def contribution_keyring() -> dict[str, str]:
    settings = get_settings()
    keys = (settings.privacy_secret_key, *settings.previous_privacy_secret_keys)
    return {contribution_key_id(key): key for key in keys if key}


def trip_contribution_hash(trip: Trip) -> str | None:
    if trip.contribution_key_id is None:
        return None
    key = contribution_keyring().get(trip.contribution_key_id)
    if key is None:
        raise ContributionKeyUnavailable
    return compute_contribution_hash(key, trip.id, trip.contribution_nonce)


def require_complete_contribution_keyring(session: Session) -> None:
    missing = session.scalar(
        select(Trip.contribution_key_id)
        .where(
            Trip.contribution_key_id.is_not(None),
            Trip.contribution_key_id.not_in(contribution_keyring()),
        )
        .limit(1)
    )
    if missing is not None:
        raise ContributionKeyUnavailable


def trip_contribution_hash_sql() -> ColumnElement[str]:
    """Derive a join key inside the private database without storing a link there."""
    key = case(contribution_keyring(), value=Trip.contribution_key_id)
    message = func.concat("trailforge:contrib:v1:", Trip.id, ":", Trip.contribution_nonce)
    return func.encode(func.hmac(message, key, "sha256"), "hex")
