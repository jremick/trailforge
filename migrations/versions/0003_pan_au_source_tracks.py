"""Add source_tracks_au_state Tier A table and widen Pan-AU 8-state CHECK constraints.

Revision ID: 0003_pan_au_source_tracks
Revises: 0002_planned_routes
Create Date: 2026-11-26 16:05:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003_pan_au_source_tracks"
down_revision: str | Sequence[str] | None = "0002_planned_routes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALL_8_STATES_SQL = "state IN ('VIC','NSW','ACT','TAS','QLD','WA','SA','NT')"
_MULTI_STATE_REGEX_SQL = (
    "state ~ '^(VIC|NSW|ACT|TAS|QLD|WA|SA|NT)(/(VIC|NSW|ACT|TAS|QLD|WA|SA|NT))*$'"
)
_ALL_9_TRACK_SOURCES_SQL = (
    "primary_source IN ('osm','vic','nsw','act','tas','qld','wa','sa','nt')"
)
_ALL_11_CLOSURE_FEEDS_SQL = (
    "source_feed IN ('vic_mapshare_seasonal','vic_emergency','nsw_rfs','nsw_npws','osm_access',"
    "'act_esa','tas_alert','qld_des_alerts','wa_emergency','sa_cfs_parks','nt_securent')"
)
_ALL_POI_LICENSES_SQL = "source_license IN ('CC-BY-4.0','CC-BY-3.0-AU','ODbL')"
_ALL_POI_PROVIDERS_SQL = (
    "source_provider IN ("
    "'vic_foi','vic_recweb_hut','vic_recweb_site','vic_geomark',"
    "'nsw_poi','nsw_trig','nsw_hut',"
    "'act_heritage_hut','act_feature_name','act_bbq','act_shelter','act_trig',"
    "'tas_hut','tas_nomenclature','tas_infra','tas_picnic','tas_trig',"
    "'qld_tourist','qld_survey',"
    "'wa_ratis_site','wa_ratis_facility','wa_fire_tower','wa_geodetic',"
    "'sa_parks','sa_survey_mark',"
    "'nt_placename','nt_survey_mark',"
    "'ga_hydro_pt','osm')"
)

_V1_STATES_SQL = "state IN ('VIC','NSW','ACT')"
_V1_TRACK_STATES_SQL = "state IN ('VIC','NSW','ACT','VIC/NSW')"
_V1_TRACK_SOURCES_SQL = "primary_source IN ('osm','vic','nsw')"
_V1_CLOSURE_FEEDS_SQL = (
    "source_feed IN ('vic_mapshare_seasonal','vic_emergency','nsw_rfs','nsw_npws','osm_access')"
)
_V1_POI_LICENSES_SQL = "source_license IN ('CC-BY-4.0','ODbL')"
_V1_POI_PROVIDERS_SQL = (
    "source_provider IN ('vic_foi','vic_recweb_hut','vic_recweb_site','vic_geomark',"
    "'nsw_poi','nsw_trig','nsw_hut','ga_hydro_pt','osm')"
)


def _replace_check(table_name: str, constraint_name: str, condition: str) -> None:
    op.drop_constraint(op.f(constraint_name), table_name, type_="check")
    op.create_check_constraint(op.f(constraint_name), table_name, condition)


def upgrade() -> None:
    # 1. Strict Tier A source table for ACT / TAS / QLD / WA / SA / NT official state tracks
    #    (keeps source_tracks_vic & source_tracks_nsw 100% untouched for backward compatibility)
    op.create_table(
        "source_tracks_au_state",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("state_code", sa.String(length=4), nullable=False),
        sa.Column("source_layer", sa.String(length=64), nullable=False),
        sa.Column("source_feature_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("trail_class", sa.Text(), nullable=False),
        sa.Column("surface_raw", sa.Text(), nullable=True),
        sa.Column("access_raw", sa.Text(), nullable=True),
        sa.Column(
            "properties_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="LINESTRING",
                srid=4326,
                from_text="ST_GeomFromEWKT",
                name="geometry",
                spatial_index=False,
                nullable=False,
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
            "state_code IN ('ACT','TAS','QLD','WA','SA','NT')",
            name="state_code_valid",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_tracks_au_state"),
        sa.UniqueConstraint(
            "state_code",
            "source_layer",
            "source_feature_id",
            name="uq_source_tracks_au_state_feat",
        ),
    )
    op.create_index(
        "ix_source_tracks_au_state_geom",
        "source_tracks_au_state",
        ["geom"],
        unique=False,
        postgresql_using="gist",
    )
    op.create_index(
        "ix_source_tracks_au_state_state_code",
        "source_tracks_au_state",
        ["state_code"],
        unique=False,
    )
    op.create_index(
        "ix_source_tracks_au_state_trail_class",
        "source_tracks_au_state",
        ["trail_class"],
        unique=False,
    )

    # 2. Widen existing CHECK constraints to all 8 AU states, 9 track sources, 11 closure feeds,
    #    and pan-AU POI providers / licenses (CC BY 3.0 AU for Tasmania LIST)
    _replace_check("source_pois", "ck_source_pois_source_provider_valid", _ALL_POI_PROVIDERS_SQL)
    _replace_check("source_pois", "ck_source_pois_source_license_valid", _ALL_POI_LICENSES_SQL)
    _replace_check("source_pois", "ck_source_pois_state_valid", _ALL_8_STATES_SQL)

    _replace_check("capad_parks", "ck_capad_parks_state_valid", _ALL_8_STATES_SQL)

    _replace_check("tracks", "ck_tracks_state_valid", _MULTI_STATE_REGEX_SQL)
    _replace_check("tracks", "ck_tracks_primary_source_valid", _ALL_9_TRACK_SOURCES_SQL)

    _replace_check("track_segments", "ck_track_segments_state_valid", _ALL_8_STATES_SQL)
    _replace_check(
        "track_segments", "ck_track_segments_primary_source_valid", _ALL_9_TRACK_SOURCES_SQL
    )

    _replace_check("pois", "ck_pois_state_valid", _ALL_8_STATES_SQL)
    _replace_check("pois", "ck_pois_source_license_valid", _ALL_POI_LICENSES_SQL)

    _replace_check(
        "closures_alerts", "ck_closures_alerts_source_feed_valid", _ALL_11_CLOSURE_FEEDS_SQL
    )
    _replace_check("closures_alerts", "ck_closures_alerts_state_valid", _MULTI_STATE_REGEX_SQL)


def downgrade() -> None:
    _replace_check("closures_alerts", "ck_closures_alerts_state_valid", _V1_STATES_SQL)
    _replace_check(
        "closures_alerts", "ck_closures_alerts_source_feed_valid", _V1_CLOSURE_FEEDS_SQL
    )

    _replace_check("pois", "ck_pois_source_license_valid", _V1_POI_LICENSES_SQL)
    _replace_check("pois", "ck_pois_state_valid", _V1_STATES_SQL)

    _replace_check(
        "track_segments", "ck_track_segments_primary_source_valid", _V1_TRACK_SOURCES_SQL
    )
    _replace_check("track_segments", "ck_track_segments_state_valid", _V1_STATES_SQL)

    _replace_check("tracks", "ck_tracks_primary_source_valid", _V1_TRACK_SOURCES_SQL)
    _replace_check("tracks", "ck_tracks_state_valid", _V1_TRACK_STATES_SQL)

    _replace_check("capad_parks", "ck_capad_parks_state_valid", _V1_STATES_SQL)

    _replace_check("source_pois", "ck_source_pois_state_valid", _V1_STATES_SQL)
    _replace_check("source_pois", "ck_source_pois_source_license_valid", _V1_POI_LICENSES_SQL)
    _replace_check("source_pois", "ck_source_pois_source_provider_valid", _V1_POI_PROVIDERS_SQL)

    op.drop_index("ix_source_tracks_au_state_trail_class", table_name="source_tracks_au_state")
    op.drop_index("ix_source_tracks_au_state_state_code", table_name="source_tracks_au_state")
    op.drop_index("ix_source_tracks_au_state_geom", table_name="source_tracks_au_state")
    op.drop_table("source_tracks_au_state")
