"""Operator-attested community grading through the normal CLI and HTTP entry points."""

from collections.abc import Generator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from geoalchemy2.shape import from_shape
from shapely.geometry import LineString
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from trailforge.api.main import create_app
from trailforge.auth.cli import main as auth_cli
from trailforge.auth.security import create_access_token
from trailforge.config import get_settings
from trailforge.db.models import (
    TelemetryObservation,
    Track,
    TrackSegment,
    TrackSegmentGrade,
    Trip,
    User,
)
from trailforge.db.session import get_db
from trailforge.matching.contribution_keys import contribution_key_id
from trailforge.matching.privacy import compute_contribution_hash


@pytest.mark.integration
def test_only_current_attestation_can_influence_persisted_grades(postgis_engine: Engine) -> None:
    track_id = f"trk_trust_{uuid4().hex}"
    segment_id = f"seg_trust_{uuid4().hex}"
    fixture_id = uuid4().hex[:10]
    now = datetime.now(UTC)
    geom = from_shape(LineString([(148.11, -36.58), (148.111, -36.58)]), srid=4326)
    key = get_settings().privacy_secret_key
    owners = [
        User(
            id=uuid4(),
            email=f"community-trust-{fixture_id}-{idx}@example.test",
            display_name=f"Fixture Member {idx}",
            password_hash="offline-fixture",
            role="member",
            is_active=True,
        )
        for idx in range(4)
    ]
    operator = User(
        id=uuid4(),
        email=f"community-trust-operator-{fixture_id}@example.test",
        display_name="Fixture Operator",
        password_hash="offline-fixture",
        role="admin",
        is_active=True,
    )

    with Session(postgis_engine) as db:
        admin_headers = {
            "Authorization": f"Bearer {create_access_token(str(operator.id), operator.email)}"
        }

        def add_observation(owner: User, *, speed_kmh: float) -> None:
            trip_id, nonce = uuid4(), uuid4()
            db.add(Trip(
                id=trip_id,
                user_id=owner.id,
                visibility="community_anonymous",
                contribution_nonce=nonce,
                contribution_key_id=contribution_key_id(key),
                community_consent_confirmed=True,
                vehicle_capability_class="Touring 4WD",
                raw_geom=geom,
            ))
            db.add(TelemetryObservation(
                segment_id=segment_id,
                contribution_hash=compute_contribution_hash(key, trip_id, nonce),
                vehicle_capability_class="Touring 4WD",
                direction="forward",
                observed_at=now,
                observed_date=now.date(),
                traversal_duration_s=20,
                avg_speed_kmh=speed_kmh,
                match_confidence=0.9,
                matched_point_count=5,
            ))

        db.add_all([operator, *owners, Track(
            track_id=track_id,
            name=f"Attested Tour {fixture_id}",
            state="VIC",
            primary_source="osm",
            trail_class="4wd_track",
        )])
        db.flush()
        db.add(TrackSegment(
            segment_id=segment_id,
            track_id=track_id,
            geom=geom,
            geom_fingerprint=uuid4().hex,
            start_node_key=f"attested_start_{fixture_id}",
            end_node_key=f"attested_end_{fixture_id}",
            length_m=90,
            bearing_deg=90,
            state="VIC",
            primary_source="osm",
            trail_class="4wd_track",
            is_active=True,
            is_vehicular=True,
        ))
        db.flush()
        db.add(TrackSegmentGrade(
            segment_id=segment_id,
            baseline_grade="Medium",
            baseline_score=42,
            baseline_breakdown={},
            effective_grade="Medium",
        ))
        db.flush()
        for owner in owners:
            add_observation(owner, speed_kmh=2)
        db.commit()

        app = create_app()

        def override_db() -> Generator[Session, None, None]:
            yield db

        app.dependency_overrides[get_db] = override_db

        try:
            with TestClient(app) as client:
                def recompute() -> TrackSegmentGrade:
                    response = client.post("/api/grading/recompute", headers=admin_headers, json={})
                    assert response.status_code == 200, response.text
                    grade = db.get(TrackSegmentGrade, segment_id)
                    assert grade is not None
                    return grade

                def attest(action: str, owner: User) -> None:
                    assert auth_cli(
                        ["community-trust", action, "--email", owner.email], session=db
                    ) == 0
                    assert owner.role == "member"

                assert recompute().sample_count == 0
                for owner in owners[:3]:
                    attest("grant", owner)
                assert recompute().sample_count == 0

                for owner in owners[:3]:
                    add_observation(owner, speed_kmh=20)
                add_observation(owners[3], speed_kmh=1)
                db.commit()
                grade = recompute()
                assert grade.sample_count == 3
                assert grade.avg_speed_kmh == 20

                attest("revoke", owners[0])
                grade = db.get(TrackSegmentGrade, segment_id)
                assert grade is not None and grade.sample_count == 0
                assert grade.live_breakdown is None and grade.avg_speed_kmh is None
                assert db.scalar(select(func.count()).where(
                    TelemetryObservation.segment_id == segment_id
                )) == 8

                attest("grant", owners[0])
                assert recompute().sample_count == 0
                add_observation(owners[0], speed_kmh=20)
                db.commit()
                assert recompute().sample_count == 3

                suspension = client.patch(
                    f"/api/admin/users/{owners[1].id}",
                    headers=admin_headers,
                    json={"is_active": False},
                )
                assert suspension.status_code == 200, suspension.text
                grade = db.get(TrackSegmentGrade, segment_id)
                assert grade is not None and grade.sample_count == 0
                assert owners[1].community_trusted_at is None
        finally:
            db.rollback()
            db.execute(delete(TelemetryObservation).where(
                TelemetryObservation.segment_id == segment_id
            ))
            db.execute(delete(TrackSegmentGrade).where(TrackSegmentGrade.segment_id == segment_id))
            db.execute(delete(TrackSegment).where(TrackSegment.segment_id == segment_id))
            db.execute(delete(Track).where(Track.track_id == track_id))
            fixture_users = [operator.id, *(owner.id for owner in owners)]
            db.execute(delete(User).where(User.id.in_(fixture_users)))
            db.commit()
