"""Initial PostGIS spatial schema covering Slices 01-09 with strict OSM ODbL separation.

Revision ID: 0001_initial_spatial_schema
Revises: None
Create Date: 2026-11-26 11:15:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial_spatial_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # -------------------------------------------------------------------------
    # Tier A — Upstream Source Tables (6 Tables, Strict ODbL Isolation)
    # -------------------------------------------------------------------------
    op.create_table(
        "source_tracks_osm",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("osm_way_id", sa.BigInteger(), nullable=False),
        sa.Column("osm_version", sa.Integer(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("highway", sa.Text(), nullable=False),
        sa.Column("surface", sa.Text(), nullable=True),
        sa.Column("tracktype", sa.Text(), nullable=True),
        sa.Column("smoothness", sa.Text(), nullable=True),
        sa.Column("four_wd_only", sa.Text(), nullable=True),
        sa.Column("access", sa.Text(), nullable=True),
        sa.Column("motor_vehicle", sa.Text(), nullable=True),
        sa.Column("bridge", sa.Text(), nullable=True),
        sa.Column("ford", sa.Text(), nullable=True),
        sa.Column(
            "has_gate_barrier", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="LINESTRING", srid=4326, spatial_index=False
            ),
            nullable=False,
        ),
        sa.Column(
            "source_snapshot_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_tracks_osm"),
        sa.UniqueConstraint("osm_way_id", name="uq_source_tracks_osm_osm_way_id"),
    )
    op.create_index(
        "ix_source_tracks_osm_geom", "source_tracks_osm", ["geom"], postgresql_using="gist"
    )
    op.create_index("ix_source_tracks_osm_highway", "source_tracks_osm", ["highway"])

    op.create_table(
        "source_tracks_vic",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_layer", sa.Text(), nullable=False),
        sa.Column("source_feature_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("trail_class", sa.Text(), nullable=False),
        sa.Column("class_code", sa.Integer(), nullable=True),
        sa.Column("surface", sa.Text(), nullable=True),
        sa.Column("is_vehicular", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "raw_props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="MULTILINESTRING", srid=4326, spatial_index=False
            ),
            nullable=False,
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_layer IN ('tr_road','recweb_tracks')",
            name="source_layer_valid",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_tracks_vic"),
        sa.UniqueConstraint(
            "source_layer", "source_feature_id", name="uq_source_tracks_vic_feature"
        ),
    )
    op.create_index(
        "ix_source_tracks_vic_geom", "source_tracks_vic", ["geom"], postgresql_using="gist"
    )
    op.create_index("ix_source_tracks_vic_trail_class", "source_tracks_vic", ["trail_class"])
    op.create_index("ix_source_tracks_vic_source_layer", "source_tracks_vic", ["source_layer"])

    op.create_table(
        "source_tracks_nsw",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_layer", sa.Text(), nullable=False),
        sa.Column("source_feature_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("trail_class", sa.Text(), nullable=False),
        sa.Column("class_subtype", sa.Integer(), nullable=True),
        sa.Column("surface", sa.Text(), nullable=True),
        sa.Column("operational_status", sa.Text(), nullable=True),
        sa.Column("is_vehicular", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "raw_props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="MULTILINESTRING", srid=4326, spatial_index=False
            ),
            nullable=False,
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_layer IN ('nsw_fire_trails_l9','nsw_tracks_l5')",
            name="source_layer_valid",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_tracks_nsw"),
        sa.UniqueConstraint(
            "source_layer", "source_feature_id", name="uq_source_tracks_nsw_feature"
        ),
    )
    op.create_index(
        "ix_source_tracks_nsw_geom", "source_tracks_nsw", ["geom"], postgresql_using="gist"
    )
    op.create_index("ix_source_tracks_nsw_trail_class", "source_tracks_nsw", ["trail_class"])
    op.create_index("ix_source_tracks_nsw_source_layer", "source_tracks_nsw", ["source_layer"])

    op.create_table(
        "source_waterways",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_provider", sa.Text(), nullable=False),
        sa.Column("source_license", sa.Text(), nullable=False),
        sa.Column("source_feature_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("feature_type", sa.Text(), nullable=False),
        sa.Column(
            "perenniality", sa.Text(), server_default=sa.text("'Unknown'"), nullable=False
        ),
        sa.Column("hierarchy", sa.Text(), nullable=True),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_provider IN ('ga_hydro','osm')",
            name="source_provider_valid",
        ),
        sa.CheckConstraint(
            "source_license IN ('CC-BY-4.0','ODbL')",
            name="source_license_valid",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_waterways"),
        sa.UniqueConstraint(
            "source_provider", "source_feature_id", name="uq_source_waterways_feat"
        ),
    )
    op.create_index(
        "ix_source_waterways_geom", "source_waterways", ["geom"], postgresql_using="gist"
    )
    op.create_index("ix_source_waterways_perenniality", "source_waterways", ["perenniality"])
    op.create_index("ix_source_waterways_source_provider", "source_waterways", ["source_provider"])

    op.create_table(
        "source_pois",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_provider", sa.Text(), nullable=False),
        sa.Column("source_license", sa.Text(), nullable=False),
        sa.Column("source_feature_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("poi_type", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("elev_m", sa.Double(), nullable=True),
        sa.Column(
            "raw_props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_provider IN ('vic_foi','vic_recweb_hut','vic_recweb_site','vic_geomark',"
            "'nsw_poi','nsw_trig','nsw_hut','ga_hydro_pt','osm')",
            name="source_provider_valid",
        ),
        sa.CheckConstraint(
            "source_license IN ('CC-BY-4.0','ODbL')",
            name="source_license_valid",
        ),
        sa.CheckConstraint(
            "poi_type IN ('campground','hut','trig','lookout','picnic','water_point',"
            "'gate','trailhead','fuel')",
            name="poi_type_valid",
        ),
        sa.CheckConstraint("state IN ('VIC','NSW','ACT')", name="state_valid"),
        sa.PrimaryKeyConstraint("id", name="pk_source_pois"),
        sa.UniqueConstraint(
            "source_provider", "source_feature_id", name="uq_source_pois_feature"
        ),
    )
    op.create_index("ix_source_pois_geom", "source_pois", ["geom"], postgresql_using="gist")
    op.create_index("ix_source_pois_poi_type", "source_pois", ["poi_type"])
    op.create_index("ix_source_pois_state", "source_pois", ["state"])

    op.create_table(
        "source_capad_parks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_feature_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("park_type", sa.Text(), nullable=False),
        sa.Column("type_abbr", sa.Text(), nullable=True),
        sa.Column("iucn", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False
            ),
            nullable=False,
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_capad_parks"),
        sa.UniqueConstraint("source_feature_id", name="uq_source_capad_parks_source_feature_id"),
    )
    op.create_index(
        "ix_source_capad_parks_geom", "source_capad_parks", ["geom"], postgresql_using="gist"
    )
    op.create_index("ix_source_capad_parks_state_name", "source_capad_parks", ["state", "name"])

    # -------------------------------------------------------------------------
    # Tier B — Canonical Conflated Network & Reference Tables (5 Tables)
    # -------------------------------------------------------------------------
    op.create_table(
        "capad_parks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("capad_external_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("park_type", sa.Text(), nullable=False),
        sa.Column("type_abbr", sa.Text(), nullable=True),
        sa.Column("iucn", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("area_ha", sa.Double(), nullable=True),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False
            ),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("state IN ('VIC','NSW','ACT')", name="state_valid"),
        sa.PrimaryKeyConstraint("id", name="pk_capad_parks"),
        sa.UniqueConstraint("capad_external_id", name="uq_capad_parks_capad_external_id"),
    )
    op.create_index("ix_capad_parks_geom", "capad_parks", ["geom"], postgresql_using="gist")
    op.create_index(
        "ix_capad_parks_state_lower_name", "capad_parks", ["state", sa.text("lower(name)")]
    )

    op.create_table(
        "tracks",
        sa.Column("track_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "alt_names",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("primary_source", sa.Text(), nullable=False),
        sa.Column(
            "has_osm_source", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("trail_class", sa.Text(), nullable=False),
        sa.Column("is_vehicular", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("total_length_m", sa.Double(), server_default=sa.text("0"), nullable=False),
        sa.Column("elev_min_m", sa.Double(), nullable=True),
        sa.Column("elev_max_m", sa.Double(), nullable=True),
        sa.Column("elev_gain_m", sa.Double(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_slope_deg", sa.Double(), nullable=True),
        sa.Column(
            "waterway_crossing_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("primary_park_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "bbox_geom",
            geoalchemy2.types.Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False),
            nullable=True,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("state IN ('VIC','NSW','ACT','VIC/NSW')", name="state_valid"),
        sa.CheckConstraint(
            "primary_source IN ('osm','vic','nsw')", name="primary_source_valid"
        ),
        sa.CheckConstraint(
            "trail_class IN ('4wd_track','fire_trail','management_trail',"
            "'unsealed_road','walking_track')",
            name="trail_class_valid",
        ),
        sa.ForeignKeyConstraint(
            ["primary_park_id"],
            ["capad_parks.id"],
            name="fk_tracks_primary_park_id_capad_parks",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("track_id", name="pk_tracks"),
    )
    op.create_index("ix_tracks_bbox_geom", "tracks", ["bbox_geom"], postgresql_using="gist")
    op.create_index("ix_tracks_state_is_active", "tracks", ["state", "is_active"])
    op.create_index("ix_tracks_lower_name", "tracks", [sa.text("lower(name)")])
    op.create_index("ix_tracks_alt_names", "tracks", ["alt_names"], postgresql_using="gin")

    op.create_table(
        "track_segments",
        sa.Column("segment_id", sa.Text(), nullable=False),
        sa.Column("track_id", sa.Text(), nullable=True),
        sa.Column("segment_seq", sa.Integer(), nullable=True),
        sa.Column("geom_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="LINESTRING", srid=4326, spatial_index=False
            ),
            nullable=False,
        ),
        sa.Column("start_node_key", sa.Text(), nullable=False),
        sa.Column("end_node_key", sa.Text(), nullable=False),
        sa.Column("length_m", sa.Double(), nullable=False),
        sa.Column("bearing_deg", sa.Double(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("trail_class", sa.Text(), nullable=False),
        sa.Column("is_vehicular", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "surface_class", sa.Text(), server_default=sa.text("'unknown'"), nullable=False
        ),
        sa.Column("tracktype", sa.Text(), nullable=True),
        sa.Column("is_4wd_only", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "is_4wd_recommended", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("access_status", sa.Text(), server_default=sa.text("'public'"), nullable=False),
        sa.Column(
            "has_gate_barrier", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("is_bridged", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("elev_min_m", sa.Double(), nullable=True),
        sa.Column("elev_max_m", sa.Double(), nullable=True),
        sa.Column("elev_start_m", sa.Double(), nullable=True),
        sa.Column("elev_end_m", sa.Double(), nullable=True),
        sa.Column("elev_gain_m", sa.Double(), server_default=sa.text("0"), nullable=False),
        sa.Column("elev_loss_m", sa.Double(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_slope_deg", sa.Double(), nullable=True),
        sa.Column("sustained_slope_deg", sa.Double(), nullable=True),
        sa.Column(
            "is_steep_highlight", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "is_above_snowline", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "elevation_profile_m",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("dem_tier", sa.Text(), nullable=True),
        sa.Column(
            "waterway_crossing_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "has_perennial_crossing", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("capad_park_id", sa.BigInteger(), nullable=True),
        sa.Column("primary_source", sa.Text(), nullable=False),
        sa.Column(
            "has_osm_source", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "source_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_segment_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length_m > 0", name="length_positive"),
        sa.CheckConstraint(
            "bearing_deg >= 0 AND bearing_deg < 360", name="bearing_range"
        ),
        sa.CheckConstraint("state IN ('VIC','NSW','ACT')", name="state_valid"),
        sa.CheckConstraint(
            "trail_class IN ('4wd_track','fire_trail','management_trail',"
            "'unsealed_road','walking_track')",
            name="trail_class_valid",
        ),
        sa.CheckConstraint(
            "surface_class IN ('sealed','gravel_compacted','dirt_unsealed',"
            "'rough_rock_mud_sand','unknown')",
            name="surface_class_valid",
        ),
        sa.CheckConstraint(
            "tracktype IN ('grade1','grade2','grade3','grade4','grade5')",
            name="tracktype_valid",
        ),
        sa.CheckConstraint(
            "access_status IN ('public','seasonal','permit','private','restricted','closed')",
            name="access_status_valid",
        ),
        sa.CheckConstraint(
            "dem_tier IN ('5m-lidar','srtm-1s','synthetic')",
            name="dem_tier_valid",
        ),
        sa.CheckConstraint(
            "primary_source IN ('osm','vic','nsw')",
            name="primary_source_valid",
        ),
        sa.ForeignKeyConstraint(
            ["track_id"],
            ["tracks.track_id"],
            name="fk_track_segments_track_id_tracks",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["capad_park_id"],
            ["capad_parks.id"],
            name="fk_track_segments_capad_park_id_capad_parks",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_segment_id"],
            ["track_segments.segment_id"],
            name="fk_track_segments_superseded_by_segment_id_track_segments",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("segment_id", name="pk_track_segments"),
    )
    op.create_index("ix_track_segments_geom", "track_segments", ["geom"], postgresql_using="gist")
    op.create_index(
        "ix_track_segments_geom_active",
        "track_segments",
        ["geom"],
        postgresql_using="gist",
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_index("ix_track_segments_geom_fingerprint", "track_segments", ["geom_fingerprint"])
    op.create_index("ix_track_segments_track_id_seq", "track_segments", ["track_id", "segment_seq"])
    op.create_index("ix_track_segments_start_node_key", "track_segments", ["start_node_key"])
    op.create_index("ix_track_segments_end_node_key", "track_segments", ["end_node_key"])
    op.create_index("ix_track_segments_capad_park_id", "track_segments", ["capad_park_id"])
    op.create_index(
        "ix_track_segments_active_vehicular_state",
        "track_segments",
        ["is_active", "is_vehicular", "state"],
    )

    op.create_table(
        "segment_reconciliation_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("import_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reconcile_action", sa.Text(), nullable=False),
        sa.Column("old_segment_id", sa.Text(), nullable=True),
        sa.Column("new_segment_id", sa.Text(), nullable=True),
        sa.Column("overlap_ratio", sa.Double(), nullable=True),
        sa.Column("hausdorff_m", sa.Double(), nullable=True),
        sa.Column("heading_diff_deg", sa.Double(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reconcile_action IN ('created','exact_match','spatial_preserved',"
            "'split','merge','retired')",
            name="reconcile_action_valid",
        ),
        sa.ForeignKeyConstraint(
            ["old_segment_id"],
            ["track_segments.segment_id"],
            name="fk_segment_reconciliation_log_old_segment_id_track_segments",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["new_segment_id"],
            ["track_segments.segment_id"],
            name="fk_segment_reconciliation_log_new_segment_id_track_segments",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_segment_reconciliation_log"),
    )
    op.create_index(
        "ix_segment_reconciliation_log_import_run_id",
        "segment_reconciliation_log",
        ["import_run_id"],
    )
    op.create_index(
        "ix_segment_reconciliation_log_old_segment_id",
        "segment_reconciliation_log",
        ["old_segment_id"],
    )
    op.create_index(
        "ix_segment_reconciliation_log_new_segment_id",
        "segment_reconciliation_log",
        ["new_segment_id"],
    )

    op.create_table(
        "pois",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("poi_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("poi_type", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("elev_m", sa.Double(), nullable=True),
        sa.Column("access_note", sa.Text(), nullable=True),
        sa.Column("nearest_segment_id", sa.Text(), nullable=True),
        sa.Column("capad_park_id", sa.BigInteger(), nullable=True),
        sa.Column("primary_source", sa.Text(), nullable=False),
        sa.Column("source_license", sa.Text(), nullable=False),
        sa.Column(
            "source_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "poi_type IN ('campground','hut','trig','lookout','picnic','water_point',"
            "'gate','trailhead','fuel')",
            name="poi_type_valid",
        ),
        sa.CheckConstraint("state IN ('VIC','NSW','ACT')", name="state_valid"),
        sa.CheckConstraint(
            "source_license IN ('CC-BY-4.0','ODbL')", name="source_license_valid"
        ),
        sa.ForeignKeyConstraint(
            ["nearest_segment_id"],
            ["track_segments.segment_id"],
            name="fk_pois_nearest_segment_id_track_segments",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["capad_park_id"],
            ["capad_parks.id"],
            name="fk_pois_capad_park_id_capad_parks",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pois"),
        sa.UniqueConstraint("poi_key", name="uq_pois_poi_key"),
    )
    op.create_index("ix_pois_geom", "pois", ["geom"], postgresql_using="gist")
    op.create_index("ix_pois_type_state", "pois", ["poi_type", "state"])
    op.create_index("ix_pois_lower_name", "pois", [sa.text("lower(name)")])
    op.create_index("ix_pois_nearest_segment_id", "pois", ["nearest_segment_id"])

    # -------------------------------------------------------------------------
    # Tier C — Independent Intelligence, Community & Live Tables (15 Tables)
    # -------------------------------------------------------------------------
    op.create_table(
        "track_segment_grades",
        sa.Column("segment_id", sa.Text(), nullable=False),
        sa.Column("baseline_grade", sa.Text(), nullable=False),
        sa.Column("baseline_score", sa.Double(), nullable=False),
        sa.Column("baseline_breakdown", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("live_grade", sa.Text(), nullable=True),
        sa.Column("live_score", sa.Double(), nullable=True),
        sa.Column("live_breakdown", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("effective_grade", sa.Text(), nullable=False),
        sa.Column(
            "confidence_level",
            sa.Text(),
            server_default=sa.text("'baseline_only'"),
            nullable=False,
        ),
        sa.Column("confidence_score", sa.Double(), server_default=sa.text("0.0"), nullable=False),
        sa.Column("sample_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_driven_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("avg_speed_kmh", sa.Double(), nullable=True),
        sa.Column("avg_duration_s", sa.Double(), nullable=True),
        sa.Column(
            "vehicle_class_split",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("rainfall_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("wet_risk_level", sa.Text(), server_default=sa.text("'dry'"), nullable=False),
        sa.Column(
            "baseline_computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("live_computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "baseline_grade IN ('Easy','Medium','Hard','Extreme')",
            name="baseline_grade_valid",
        ),
        sa.CheckConstraint(
            "baseline_score >= 0 AND baseline_score <= 100",
            name="baseline_score_rng",
        ),
        sa.CheckConstraint(
            "live_grade IN ('Easy','Medium','Hard','Extreme')",
            name="live_grade_valid",
        ),
        sa.CheckConstraint(
            "live_score >= 0 AND live_score <= 100",
            name="live_score_range",
        ),
        sa.CheckConstraint(
            "effective_grade IN ('Easy','Medium','Hard','Extreme')",
            name="effective_grade_valid",
        ),
        sa.CheckConstraint(
            "confidence_level IN ('baseline_only','low','medium','high')",
            name="confidence_level_valid",
        ),
        sa.CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="conf_score_range",
        ),
        sa.CheckConstraint(
            "wet_risk_level IN ('dry','damp','wet','saturated')",
            name="wet_risk_level_valid",
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"],
            ["track_segments.segment_id"],
            name="fk_track_segment_grades_segment_id_track_segments",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("segment_id", name="pk_track_segment_grades"),
    )
    op.create_index(
        "ix_track_segment_grades_effective_grade", "track_segment_grades", ["effective_grade"]
    )
    op.create_index(
        "ix_track_segment_grades_confidence_level", "track_segment_grades", ["confidence_level"]
    )
    op.create_index(
        "ix_track_segment_grades_last_driven_at",
        "track_segment_grades",
        [sa.text("last_driven_at DESC")],
    )
    op.create_index(
        "ix_track_segment_grades_wet_risk_level", "track_segment_grades", ["wet_risk_level"]
    )

    op.create_table(
        "closures_alerts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_feed", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("alert_type", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("capad_park_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
            nullable=True,
        ),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_feed IN ('vic_mapshare_seasonal','vic_emergency','nsw_rfs',"
            "'nsw_npws','osm_access')",
            name="source_feed_valid",
        ),
        sa.CheckConstraint(
            "alert_type IN ('seasonal_closure','road_closure','park_closure','bushfire',"
            "'flood','hazard_warning','private_gate')",
            name="alert_type_valid",
        ),
        sa.CheckConstraint(
            "severity IN ('info','advisory','watch_and_act','closed','emergency')",
            name="severity_valid",
        ),
        sa.CheckConstraint(
            "status IN ('active','scheduled','cleared','stale')",
            name="status_valid",
        ),
        sa.CheckConstraint("state IN ('VIC','NSW','ACT')", name="state_valid"),
        sa.ForeignKeyConstraint(
            ["capad_park_id"],
            ["capad_parks.id"],
            name="fk_closures_alerts_capad_park_id_capad_parks",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_closures_alerts"),
        sa.UniqueConstraint("source_feed", "external_id", name="uq_closures_alerts_feed_ext"),
    )
    op.create_index("ix_closures_alerts_geom", "closures_alerts", ["geom"], postgresql_using="gist")
    op.create_index(
        "ix_closures_alerts_status_state_type",
        "closures_alerts",
        ["status", "state", "alert_type"],
    )
    op.create_index("ix_closures_alerts_capad_park_id", "closures_alerts", ["capad_park_id"])
    op.create_index(
        "ix_closures_alerts_feed_last_seen",
        "closures_alerts",
        ["source_feed", sa.text("last_seen_at DESC")],
    )

    op.create_table(
        "segment_closure_links",
        sa.Column("segment_id", sa.Text(), nullable=False),
        sa.Column("closure_id", sa.BigInteger(), nullable=False),
        sa.Column("link_method", sa.Text(), nullable=False),
        sa.Column("distance_m", sa.Double(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "link_method IN ('spatial_intersect','spatial_buffer',"
            "'capad_park_join','osm_access_flag')",
            name="link_method_valid",
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"],
            ["track_segments.segment_id"],
            name="fk_segment_closure_links_segment_id_track_segments",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["closure_id"],
            ["closures_alerts.id"],
            name="fk_segment_closure_links_closure_id_closures_alerts",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("segment_id", "closure_id", name="pk_segment_closure_links"),
    )
    op.create_index(
        "ix_segment_closure_links_closure_id", "segment_closure_links", ["closure_id"]
    )
    op.create_index(
        "ix_segment_closure_links_segment_id", "segment_closure_links", ["segment_id"]
    )

    op.create_table(
        "feed_sync_status",
        sa.Column("feed_name", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("sync_interval_s", sa.Integer(), nullable=False),
        sa.Column("stale_after_s", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.Text(), server_default=sa.text("'never_run'"), nullable=False),
        sa.Column(
            "consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("records_synced", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "last_status IN ('never_run','ok','degraded','failed')",
            name="last_status_valid",
        ),
        sa.PrimaryKeyConstraint("feed_name", name="pk_feed_sync_status"),
    )

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), server_default=sa.text("'user'"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('user','moderator','admin')", name="role_valid"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_lower_email", "users", [sa.text("lower(email)")])

    op.create_table(
        "vehicle_profiles",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("drivetrain", sa.Text(), nullable=False),
        sa.Column("has_low_range", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("lift_mm", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "ground_clearance_mm", sa.Integer(), server_default=sa.text("210"), nullable=False
        ),
        sa.Column("tire_class", sa.Text(), nullable=False),
        sa.Column("front_locker", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("rear_locker", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("has_winch", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("has_snorkel", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("capability_class", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "drivetrain IN ('awd','4wd_part_time','4wd_full_time')",
            name="drivetrain_valid",
        ),
        sa.CheckConstraint("lift_mm >= 0 AND lift_mm <= 300", name="lift_range"),
        sa.CheckConstraint(
            "ground_clearance_mm >= 120 AND ground_clearance_mm <= 500",
            name="clearance_range",
        ),
        sa.CheckConstraint(
            "tire_class IN ('HT','AT','MT')", name="tire_class_valid"
        ),
        sa.CheckConstraint(
            "capability_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD')",
            name="capability_class_valid",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_vehicle_profiles_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_vehicle_profiles"),
    )
    op.create_index("ix_vehicle_profiles_user_id", "vehicle_profiles", ["user_id"])
    op.create_index(
        "uq_vehicle_profiles_user_default",
        "vehicle_profiles",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_default = true"),
    )

    op.create_table(
        "user_favorites",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("track_id", sa.Text(), nullable=True),
        sa.Column("segment_id", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "track_id IS NOT NULL OR segment_id IS NOT NULL",
            name="track_or_segment_required",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_favorites_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["track_id"],
            ["tracks.track_id"],
            name="fk_user_favorites_track_id_tracks",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"],
            ["track_segments.segment_id"],
            name="fk_user_favorites_segment_id_track_segments",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_favorites"),
    )
    op.create_index(
        "ix_user_favorites_user_created",
        "user_favorites",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "uq_user_favorites_user_track_only",
        "user_favorites",
        ["user_id", "track_id"],
        unique=True,
        postgresql_where=sa.text("segment_id IS NULL"),
    )
    op.create_index(
        "uq_user_favorites_user_segment_only",
        "user_favorites",
        ["user_id", "segment_id"],
        unique=True,
        postgresql_where=sa.text("track_id IS NULL"),
    )
    op.create_index(
        "uq_user_favorites_user_track_segment",
        "user_favorites",
        ["user_id", "track_id", "segment_id"],
        unique=True,
        postgresql_where=sa.text("track_id IS NOT NULL AND segment_id IS NOT NULL"),
    )

    op.create_table(
        "user_pins",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), server_default=sa.text("'waypoint'"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("elev_m", sa.Double(), nullable=True),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "category IN ('waypoint','camp','hazard','photo','water','recovery')",
            name="category_valid",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_pins_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_pins"),
        sa.UniqueConstraint("user_id", "client_uuid", name="uq_user_pins_user_client_uuid"),
    )
    op.create_index("ix_user_pins_geom", "user_pins", ["geom"], postgresql_using="gist")
    op.create_index(
        "ix_user_pins_user_created", "user_pins", ["user_id", sa.text("created_at DESC")]
    )

    op.create_table(
        "trips",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("vehicle_profile_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("vehicle_capability_class", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("visibility", sa.Text(), server_default=sa.text("'private'"), nullable=False),
        sa.Column("privacy_trim_m", sa.Double(), server_default=sa.text("250.0"), nullable=False),
        sa.Column(
            "contribution_nonce",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "raw_geom",
            geoalchemy2.types.Geometry(
                geometry_type="LINESTRING", srid=4326, spatial_index=False
            ),
            nullable=False,
        ),
        sa.Column(
            "trimmed_geom",
            geoalchemy2.types.Geometry(
                geometry_type="LINESTRING", srid=4326, spatial_index=False
            ),
            nullable=True,
        ),
        sa.Column("raw_distance_m", sa.Double(), server_default=sa.text("0"), nullable=False),
        sa.Column("trimmed_distance_m", sa.Double(), server_default=sa.text("0"), nullable=False),
        sa.Column("duration_s", sa.Double(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("match_status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("matcher_name", sa.Text(), nullable=True),
        sa.Column("avg_match_confidence", sa.Double(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "vehicle_capability_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD')",
            name="vehicle_class_valid",
        ),
        sa.CheckConstraint(
            "visibility IN ('private','community_anonymous')",
            name="visibility_valid",
        ),
        sa.CheckConstraint(
            "privacy_trim_m >= 250.0 AND privacy_trim_m <= 5000.0",
            name="privacy_trim_range",
        ),
        sa.CheckConstraint(
            "match_status IN ('pending','matched','partial','unmapped',"
            "'skipped_private','skipped_too_short','failed')",
            name="match_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_trips_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["vehicle_profile_id"],
            ["vehicle_profiles.id"],
            name="fk_trips_vehicle_profile_id_vehicle_profiles",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_trips"),
        sa.UniqueConstraint("user_id", "client_uuid", name="uq_trips_user_client_uuid"),
    )
    op.create_index("ix_trips_raw_geom", "trips", ["raw_geom"], postgresql_using="gist")
    op.create_index("ix_trips_trimmed_geom", "trips", ["trimmed_geom"], postgresql_using="gist")
    op.create_index("ix_trips_user_created", "trips", ["user_id", sa.text("created_at DESC")])
    op.create_index("ix_trips_match_status", "trips", ["match_status"])

    op.create_table(
        "telemetry_observations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("segment_id", sa.Text(), nullable=False),
        sa.Column("contribution_hash", sa.String(length=64), nullable=False),
        sa.Column("vehicle_capability_class", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_date", sa.Date(), nullable=False),
        sa.Column("traversal_duration_s", sa.Double(), nullable=False),
        sa.Column("avg_speed_kmh", sa.Double(), nullable=False),
        sa.Column("min_speed_kmh", sa.Double(), nullable=True),
        sa.Column("roughness_rms", sa.Double(), nullable=True),
        sa.Column("roughness_p95", sa.Double(), nullable=True),
        sa.Column("match_confidence", sa.Double(), nullable=False),
        sa.Column("matched_point_count", sa.Integer(), nullable=False),
        sa.Column(
            "slow_hotspot_geom",
            geoalchemy2.types.Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=True,
        ),
        sa.Column("rainfall_72h_mm", sa.Double(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "vehicle_capability_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD')",
            name="vehicle_class_valid",
        ),
        sa.CheckConstraint(
            "direction IN ('forward','reverse')",
            name="direction_valid",
        ),
        sa.CheckConstraint(
            "traversal_duration_s > 0", name="duration_positive"
        ),
        sa.CheckConstraint(
            "avg_speed_kmh >= 0 AND avg_speed_kmh <= 160",
            name="avg_speed_range",
        ),
        sa.CheckConstraint(
            "min_speed_kmh >= 0 AND min_speed_kmh <= 160",
            name="min_speed_range",
        ),
        sa.CheckConstraint(
            "roughness_rms >= 0", name="roughness_rms_nonneg"
        ),
        sa.CheckConstraint(
            "roughness_p95 >= 0", name="roughness_p95_nonneg"
        ),
        sa.CheckConstraint(
            "match_confidence >= 0 AND match_confidence <= 1",
            name="match_confidence_range",
        ),
        sa.CheckConstraint(
            "matched_point_count >= 1", name="matched_point_count_min"
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"],
            ["track_segments.segment_id"],
            name="fk_telemetry_observations_segment_id_track_segments",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_telemetry_observations"),
    )
    op.create_index(
        "ix_telemetry_observations_segment_observed",
        "telemetry_observations",
        ["segment_id", sa.text("observed_at DESC")],
    )
    op.create_index(
        "ix_telemetry_observations_contribution_hash",
        "telemetry_observations",
        ["contribution_hash"],
    )
    op.create_index(
        "ix_telemetry_observations_segment_vehicle",
        "telemetry_observations",
        ["segment_id", "vehicle_capability_class"],
    )
    op.create_index(
        "ix_telemetry_observations_slow_hotspot_geom",
        "telemetry_observations",
        ["slow_hotspot_geom"],
        postgresql_using="gist",
        postgresql_where=sa.text("slow_hotspot_geom IS NOT NULL"),
    )

    op.create_table(
        "unmapped_track_candidates",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("contribution_hash", sa.String(length=64), nullable=False),
        sa.Column("vehicle_capability_class", sa.Text(), nullable=False),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="LINESTRING", srid=4326, spatial_index=False
            ),
            nullable=False,
        ),
        sa.Column("length_m", sa.Double(), nullable=False),
        sa.Column("observed_date", sa.Date(), nullable=False),
        sa.Column("cluster_key", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'pending_review'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "vehicle_capability_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD')",
            name="vehicle_class_valid",
        ),
        sa.CheckConstraint("length_m > 0", name="length_positive"),
        sa.CheckConstraint(
            "status IN ('pending_review','corroborated','promoted','rejected')",
            name="status_valid",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_unmapped_track_candidates"),
    )
    op.create_index(
        "ix_unmapped_track_candidates_geom",
        "unmapped_track_candidates",
        ["geom"],
        postgresql_using="gist",
    )
    op.create_index(
        "ix_unmapped_track_candidates_contribution_hash",
        "unmapped_track_candidates",
        ["contribution_hash"],
    )
    op.create_index(
        "ix_unmapped_track_candidates_status_cluster",
        "unmapped_track_candidates",
        ["status", "cluster_key"],
    )

    op.create_table(
        "condition_reports",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("segment_id", sa.Text(), nullable=True),
        sa.Column("track_id", sa.Text(), nullable=True),
        sa.Column("report_type", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), server_default=sa.text("'caution'"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("photo_url", sa.Text(), nullable=True),
        sa.Column("vehicle_capability_class", sa.Text(), nullable=True),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("confirm_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("resolve_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("decay_score", sa.Double(), server_default=sa.text("1.0"), nullable=False),
        sa.Column("osm_edit_url", sa.Text(), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "report_type IN ('fallen_tree','bog_hole','washout','deep_water',"
            "'locked_gate','private_property','snow_ice','track_clear')",
            name="report_type_valid",
        ),
        sa.CheckConstraint(
            "severity IN ('info','caution','impassable')",
            name="severity_valid",
        ),
        sa.CheckConstraint(
            "status IN ('active','resolved','stale','hidden')",
            name="status_valid",
        ),
        sa.CheckConstraint(
            "vehicle_capability_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD')",
            name="vehicle_class_valid",
        ),
        sa.CheckConstraint(
            "decay_score >= 0 AND decay_score <= 1",
            name="decay_score_range",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_condition_reports_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"],
            ["track_segments.segment_id"],
            name="fk_condition_reports_segment_id_track_segments",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["track_id"],
            ["tracks.track_id"],
            name="fk_condition_reports_track_id_tracks",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_condition_reports"),
        sa.UniqueConstraint(
            "user_id", "client_uuid", name="uq_condition_reports_user_client_uuid"
        ),
    )
    op.create_index(
        "ix_condition_reports_geom", "condition_reports", ["geom"], postgresql_using="gist"
    )
    op.create_index(
        "ix_condition_reports_segment_status_observed",
        "condition_reports",
        ["segment_id", "status", sa.text("observed_at DESC")],
    )
    op.create_index(
        "ix_condition_reports_type_status", "condition_reports", ["report_type", "status"]
    )
    op.create_index("ix_condition_reports_user_id", "condition_reports", ["user_id"])

    op.create_table(
        "condition_report_votes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vote_type", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "vote_type IN ('confirm','resolved','inaccurate')",
            name="vote_type_valid",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["condition_reports.id"],
            name="fk_condition_report_votes_report_id_condition_reports",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_condition_report_votes_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_condition_report_votes"),
        sa.UniqueConstraint(
            "report_id", "user_id", name="uq_condition_report_votes_report_user"
        ),
    )
    op.create_index(
        "ix_condition_report_votes_report_type",
        "condition_report_votes",
        ["report_id", "vote_type"],
    )

    op.create_table(
        "rainfall_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("grid_cell_id", sa.Text(), nullable=False),
        sa.Column(
            "centroid_geom",
            geoalchemy2.types.Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column(
            "source_provider", sa.Text(), server_default=sa.text("'open-meteo'"), nullable=False
        ),
        sa.Column("rain_24h_mm", sa.Double(), server_default=sa.text("0"), nullable=False),
        sa.Column("rain_72h_mm", sa.Double(), server_default=sa.text("0"), nullable=False),
        sa.Column("rain_7d_mm", sa.Double(), nullable=True),
        sa.Column("wet_risk_level", sa.Text(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("rain_24h_mm >= 0", name="rain_24h_nonneg"),
        sa.CheckConstraint("rain_72h_mm >= 0", name="rain_72h_nonneg"),
        sa.CheckConstraint("rain_7d_mm >= 0", name="rain_7d_nonneg"),
        sa.CheckConstraint(
            "wet_risk_level IN ('dry','damp','wet','saturated')",
            name="wet_risk_level_valid",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_rainfall_snapshots"),
        sa.UniqueConstraint(
            "grid_cell_id", "fetched_at", name="uq_rainfall_snapshots_cell_fetched"
        ),
    )
    op.create_index(
        "ix_rainfall_snapshots_centroid_geom",
        "rainfall_snapshots",
        ["centroid_geom"],
        postgresql_using="gist",
    )
    op.create_index(
        "ix_rainfall_snapshots_cell_fetched",
        "rainfall_snapshots",
        ["grid_cell_id", sa.text("fetched_at DESC")],
    )

    op.create_table(
        "offline_packs",
        sa.Column("pack_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("region_type", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column(
            "bbox_geom",
            geoalchemy2.types.Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column(
            "utm_zones",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("vector_pmtiles_path", sa.Text(), nullable=False),
        sa.Column("terrain_pmtiles_path", sa.Text(), nullable=True),
        sa.Column("snapshot_json_path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "attributions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default=sa.text("'building'"), nullable=False),
        sa.Column(
            "built_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "region_type IN ('tactical','state','custom')",
            name="region_type_valid",
        ),
        sa.CheckConstraint(
            "status IN ('building','ready','failed')",
            name="status_valid",
        ),
        sa.PrimaryKeyConstraint("pack_id", name="pk_offline_packs"),
    )
    op.create_index(
        "ix_offline_packs_bbox_geom", "offline_packs", ["bbox_geom"], postgresql_using="gist"
    )
    op.create_index(
        "ix_offline_packs_status_region_type", "offline_packs", ["status", "region_type"]
    )


def downgrade() -> None:
    # Drop Tier C in reverse dependency order
    op.drop_table("offline_packs")
    op.drop_table("rainfall_snapshots")
    op.drop_table("condition_report_votes")
    op.drop_table("condition_reports")
    op.drop_table("unmapped_track_candidates")
    op.drop_table("telemetry_observations")
    op.drop_table("trips")
    op.drop_table("user_pins")
    op.drop_table("user_favorites")
    op.drop_table("vehicle_profiles")
    op.drop_table("users")
    op.drop_table("feed_sync_status")
    op.drop_table("segment_closure_links")
    op.drop_table("closures_alerts")
    op.drop_table("track_segment_grades")

    # Drop Tier B in reverse dependency order
    op.drop_table("pois")
    op.drop_table("segment_reconciliation_log")
    op.drop_table("track_segments")
    op.drop_table("tracks")
    op.drop_table("capad_parks")

    # Drop Tier A
    op.drop_table("source_capad_parks")
    op.drop_table("source_pois")
    op.drop_table("source_waterways")
    op.drop_table("source_tracks_nsw")
    op.drop_table("source_tracks_vic")
    op.drop_table("source_tracks_osm")
