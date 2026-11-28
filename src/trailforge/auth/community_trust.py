"""Operator attestation for community grading and report evidence."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from trailforge.db.models import TelemetryObservation, Trip, User
from trailforge.matching.contribution_keys import (
    ContributionKeyUnavailable,
    require_complete_contribution_keyring,
    trip_contribution_hash_sql,
)


def has_active_attestation(submitted_at: ColumnElement[datetime]) -> ColumnElement[bool]:
    """Trust only evidence submitted since the account's current operator attestation."""
    return and_(User.is_active.is_(True), submitted_at >= User.community_trusted_at)


def has_active_telemetry_attestation() -> ColumnElement[bool]:
    """Check the trip as well as derived rows so rematching cannot confer authority."""
    return and_(
        has_active_attestation(Trip.created_at),
        TelemetryObservation.created_at >= User.community_trusted_at,
    )


def revoke_community_trust(session: Session, user: User) -> None:
    """Remove the current grant and refresh public grades in the same transaction.

    The caller holds a no-key-update lock on the target user.
    """
    from trailforge.grading.live import (
        clear_all_community_grades,
        lock_community_grading,
        refresh_grades_after_withdrawal,
    )

    granted_at = user.community_trusted_at
    if granted_at is None:
        return
    lock_community_grading(session)
    user.community_trusted_at = None
    session.flush()

    try:
        require_complete_contribution_keyring(session)
    except ContributionKeyUnavailable:
        # A missing key prevents precise attribution. Hide materialized evidence
        # across the instance until the operator restores the retained keys.
        clear_all_community_grades(session)
        return

    segments = session.scalars(
        select(TelemetryObservation.segment_id)
        .join(Trip, trip_contribution_hash_sql() == TelemetryObservation.contribution_hash)
        .where(
            Trip.user_id == user.id,
            Trip.visibility == "community_anonymous",
            Trip.community_consent_confirmed.is_(True),
            Trip.created_at >= granted_at,
            TelemetryObservation.created_at >= granted_at,
        )
        .distinct()
    ).all()
    refresh_grades_after_withdrawal(session, segments)
