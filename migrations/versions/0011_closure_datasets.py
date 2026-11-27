"""Scope closure snapshots to their dataset and index regional polygons.

Revision ID: 0011_closure_datasets
Revises: 0010_community_telemetry
"""

from __future__ import annotations

from hashlib import sha256

import sqlalchemy as sa
from alembic import op

revision = "0011_closure_datasets"
down_revision = "0010_community_telemetry"
branch_labels = None
depends_on = None

_QLD_FIRE = "https://www.fire.qld.gov.au/data/alerts/bushfireAlert.json"
_NT_ROADS = "https://roadreport.nt.gov.au/api/Obstruction/GetAll"


def upgrade() -> None:
    for table in ("closures_alerts", "feed_sync_status"):
        op.add_column(table, sa.Column("dataset_key", sa.Text(), nullable=False, server_default="live"))

    # Only backfill unambiguous records from the known default payload shapes. Existing
    # alerts from alternate or uncertain sources keep their IDs and links in legacy scope.
    op.execute(
        "UPDATE closures_alerts SET dataset_key = 'legacy' "
        "WHERE source_feed IN ('qld_des_alerts', 'nt_securent')"
    )
    qld_key = sha256(_QLD_FIRE.encode()).hexdigest()
    nt_key = sha256(_NT_ROADS.encode()).hexdigest()
    op.execute(
        f"UPDATE closures_alerts SET dataset_key = 'endpoint:{qld_key}' "
        "WHERE source_feed = 'qld_des_alerts' AND raw_payload ? 'UniqueID' "
        "AND raw_payload ? 'WarningTitle'"
    )
    op.execute(
        f"UPDATE closures_alerts SET dataset_key = 'endpoint:{nt_key}' "
        "WHERE source_feed = 'nt_securent' AND external_id LIKE 'nt-rr-%' "
        "AND raw_payload ? 'roadName' AND raw_payload ? 'restrictionType'"
    )
    # Historical health combined the URLs so its success timestamp has no reliable dataset.
    op.execute(
        "UPDATE feed_sync_status SET dataset_key = 'legacy' "
        "WHERE feed_name IN ('qld_des_alerts', 'nt_securent')"
    )

    op.drop_constraint("uq_closures_alerts_feed_ext", "closures_alerts", type_="unique")
    op.create_unique_constraint(
        "uq_closures_alerts_feed_ext", "closures_alerts", ["source_feed", "dataset_key", "external_id"]
    )
    op.drop_constraint("pk_feed_sync_status", "feed_sync_status", type_="primary")
    op.create_primary_key("pk_feed_sync_status", "feed_sync_status", ["feed_name", "dataset_key"])

    op.add_column(
        "closures_alerts", sa.Column("is_regional", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.execute(
        "UPDATE closures_alerts SET is_regional = true "
        "WHERE CASE WHEN geom IS NOT NULL "
        "AND GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON') "
        "AND ST_IsValid(geom) "
        "AND ST_XMin(geom) >= 90 AND ST_XMax(geom) <= 180 "
        "AND ST_YMin(geom) >= -60 AND ST_YMax(geom) <= 0 "
        "THEN ST_Area(ST_Transform(geom, 3577)) > 5000000000.0 ELSE false END"
    )
    op.create_index(
        "ix_closures_alerts_regional_geom",
        "closures_alerts",
        ["geom"],
        postgresql_using="gist",
        postgresql_where=sa.text("is_regional IS TRUE"),
    )


def downgrade() -> None:
    # Older schemas cannot represent overlapping IDs or health rows for separate datasets.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM closures_alerts GROUP BY source_feed, external_id HAVING count(*) > 1
            ) OR EXISTS (
                SELECT 1 FROM feed_sync_status GROUP BY feed_name HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'Separate closure datasets cannot be represented by the older schema';
            END IF;
        END $$
    """)
    op.drop_index("ix_closures_alerts_regional_geom", table_name="closures_alerts")
    op.drop_column("closures_alerts", "is_regional")
    op.drop_constraint("pk_feed_sync_status", "feed_sync_status", type_="primary")
    op.create_primary_key("pk_feed_sync_status", "feed_sync_status", ["feed_name"])
    op.drop_constraint("uq_closures_alerts_feed_ext", "closures_alerts", type_="unique")
    op.create_unique_constraint("uq_closures_alerts_feed_ext", "closures_alerts", ["source_feed", "external_id"])
    for table in ("closures_alerts", "feed_sync_status"):
        op.drop_column(table, "dataset_key")
