"""Check or rekey existing telemetry contributions during a privacy key rotation."""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import exists, or_, select, update
from sqlalchemy.orm import Session

from trailforge.config import get_settings
from trailforge.db.models import TelemetryObservation, Trip, UnmappedTrackCandidate
from trailforge.db.session import get_engine
from trailforge.grading.live import lock_community_grading
from trailforge.matching.contribution_keys import (
    ContributionKeyUnavailable,
    contribution_key_id,
    contribution_keyring,
    require_complete_contribution_keyring,
    trip_contribution_hash,
)
from trailforge.matching.privacy import compute_contribution_hash


def rekey_contributions(session: Session) -> int:
    """Recover available legacy references and move all linked contributions to the active key."""
    require_complete_contribution_keyring(session)
    key = get_settings().privacy_secret_key
    active_key_id = contribution_key_id(key)
    trips = session.scalars(
        select(Trip)
        .where(
            Trip.visibility == "community_anonymous",
            Trip.contribution_key_id.is_distinct_from(active_key_id),
        )
        .order_by(Trip.id)
        .with_for_update(key_share=True)
    ).all()
    lock_community_grading(session)
    updated = 0
    for trip in trips:
        old_hash = trip_contribution_hash(trip)
        if old_hash is None:
            # A retained secret may have been supplied after the upgrade.
            for retained in contribution_keyring().values():
                candidate = compute_contribution_hash(retained, trip.id, trip.contribution_nonce)
                found = session.scalar(select(or_(
                    exists().where(TelemetryObservation.contribution_hash == candidate),
                    exists().where(UnmappedTrackCandidate.contribution_hash == candidate),
                )))
                if found:
                    old_hash = candidate
                    break
        if old_hash is None:
            continue

        new_hash = compute_contribution_hash(key, trip.id, trip.contribution_nonce)
        if old_hash != new_hash:
            for model in (TelemetryObservation, UnmappedTrackCandidate):
                session.execute(
                    update(model)
                    .where(model.contribution_hash == old_hash)
                    .values(contribution_hash=new_hash)
                )
        trip.contribution_key_id = active_key_id
        updated += 1
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "rekey"))
    args = parser.parse_args(argv)
    try:
        with Session(get_engine()) as session:
            require_complete_contribution_keyring(session)
            if args.action == "rekey":
                count = rekey_contributions(session)
                session.commit()
                print(f"Updated contribution keys for {count} trips.")
            else:
                print("All recorded contribution key versions are available.")
    except ContributionKeyUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
