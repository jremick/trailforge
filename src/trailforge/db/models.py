"""SQLAlchemy 2 + GeoAlchemy2 spatial models for TrailForge (Slices 01-09 + v1.2).

Implements all 34 PostGIS tables across three strict OSM ODbL separation tiers:
  - Tier A (7 tables): Dedicated upstream source staging tables (incl. Pan-AU state tracks)
  - Tier B (5 tables): Canonical conflated network & reference tables
  - Tier C (22 tables): Independent TrailForge grading, feeds, telemetry, access & AI auth tables
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, validates

from trailforge.db.base import Base
from trailforge.db.models_ai_auth import (
    OAuthAuthorizationCode,
    OAuthClient,
    OAuthToken,
    PersonalAccessToken,
)

# =============================================================================
# Tier A — Upstream Source Tables (6 Tables, Strict ODbL Isolation)
# =============================================================================


class SourceTrackOSM(Base):
    """Dedicated OpenStreetMap way staging table (ODbL isolated)."""

    __tablename__ = "source_tracks_osm"
    __table_args__ = (
        Index("ix_source_tracks_osm_geom", "geom", postgresql_using="gist"),
        Index("ix_source_tracks_osm_highway", "highway"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    osm_way_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    osm_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    highway: Mapped[str] = mapped_column(Text, nullable=False)
    surface: Mapped[str | None] = mapped_column(Text, nullable=True)
    tracktype: Mapped[str | None] = mapped_column(Text, nullable=True)
    smoothness: Mapped[str | None] = mapped_column(Text, nullable=True)
    four_wd_only: Mapped[str | None] = mapped_column(Text, nullable=True)
    access: Mapped[str | None] = mapped_column(Text, nullable=True)
    motor_vehicle: Mapped[str | None] = mapped_column(Text, nullable=True)
    bridge: Mapped[str | None] = mapped_column(Text, nullable=True)
    ford: Mapped[str | None] = mapped_column(Text, nullable=True)
    has_gate_barrier: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    tags: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    geom: Mapped[Any] = mapped_column(
        Geometry("LINESTRING", srid=4326, spatial_index=False), nullable=False
    )
    source_snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SourceTrackVIC(Base):
    """Dedicated Victoria DataVic/Vicmap (tr_road & recweb_tracks) staging table (CC BY 4.0)."""

    __tablename__ = "source_tracks_vic"
    __table_args__ = (
        CheckConstraint(
            "source_layer IN ('tr_road','recweb_tracks')", name="source_layer_valid"
        ),
        UniqueConstraint("source_layer", "source_feature_id", name="uq_source_tracks_vic_feature"),
        Index("ix_source_tracks_vic_geom", "geom", postgresql_using="gist"),
        Index("ix_source_tracks_vic_trail_class", "trail_class"),
        Index("ix_source_tracks_vic_source_layer", "source_layer"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_layer: Mapped[str] = mapped_column(Text, nullable=False)
    source_feature_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    trail_class: Mapped[str] = mapped_column(Text, nullable=False)
    class_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    surface: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_vehicular: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    raw_props: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    geom: Mapped[Any] = mapped_column(
        Geometry("MULTILINESTRING", srid=4326, spatial_index=False), nullable=False
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SourceTrackNSW(Base):
    """Dedicated NSW Spatial Services (NSW_Transport_Theme L5 & L9) staging table (CC BY 4.0)."""

    __tablename__ = "source_tracks_nsw"
    __table_args__ = (
        CheckConstraint(
            "source_layer IN ('nsw_fire_trails_l9','nsw_tracks_l5')",
            name="source_layer_valid",
        ),
        UniqueConstraint("source_layer", "source_feature_id", name="uq_source_tracks_nsw_feature"),
        Index("ix_source_tracks_nsw_geom", "geom", postgresql_using="gist"),
        Index("ix_source_tracks_nsw_trail_class", "trail_class"),
        Index("ix_source_tracks_nsw_source_layer", "source_layer"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_layer: Mapped[str] = mapped_column(Text, nullable=False)
    source_feature_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    trail_class: Mapped[str] = mapped_column(Text, nullable=False)
    class_subtype: Mapped[int | None] = mapped_column(Integer, nullable=True)
    surface: Mapped[str | None] = mapped_column(Text, nullable=True)
    operational_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_vehicular: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    raw_props: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    geom: Mapped[Any] = mapped_column(
        Geometry("MULTILINESTRING", srid=4326, spatial_index=False), nullable=False
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SourceTrackAUState(Base):
    """Dedicated Pan-Australia (ACT/TAS/QLD/WA/SA/NT) official state tracks staging table."""

    __tablename__ = "source_tracks_au_state"
    __table_args__ = (
        CheckConstraint(
            "state_code IN ('ACT','TAS','QLD','WA','SA','NT')",
            name="state_code_valid",
        ),
        UniqueConstraint(
            "state_code",
            "source_layer",
            "source_feature_id",
            name="uq_source_tracks_au_state_feat",
        ),
        Index("ix_source_tracks_au_state_geom", "geom", postgresql_using="gist"),
        Index("ix_source_tracks_au_state_state_code", "state_code"),
        Index("ix_source_tracks_au_state_trail_class", "trail_class"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    state_code: Mapped[str] = mapped_column(String(4), nullable=False)
    source_layer: Mapped[str] = mapped_column(String(64), nullable=False)
    source_feature_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    trail_class: Mapped[str] = mapped_column(Text, nullable=False)
    surface_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    properties_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    geom: Mapped[Any] = mapped_column(
        Geometry("LINESTRING", srid=4326, spatial_index=False), nullable=False
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SourceWaterway(Base):
    """Upstream GA Surface Hydrology (CC BY 4.0) & OSM (ODbL) waterways."""

    __tablename__ = "source_waterways"
    __table_args__ = (
        CheckConstraint("source_provider IN ('ga_hydro','osm')", name="source_provider_valid"),
        CheckConstraint("source_license IN ('CC-BY-4.0','ODbL')", name="source_license_valid"),
        UniqueConstraint("source_provider", "source_feature_id", name="uq_source_waterways_feat"),
        Index("ix_source_waterways_geom", "geom", postgresql_using="gist"),
        Index("ix_source_waterways_perenniality", "perenniality"),
        Index("ix_source_waterways_source_provider", "source_provider"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_provider: Mapped[str] = mapped_column(Text, nullable=False)
    source_license: Mapped[str] = mapped_column(Text, nullable=False)
    source_feature_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    feature_type: Mapped[str] = mapped_column(Text, nullable=False)
    perenniality: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'Unknown'")
    )
    hierarchy: Mapped[str | None] = mapped_column(Text, nullable=True)
    geom: Mapped[Any] = mapped_column(
        Geometry("GEOMETRY", srid=4326, spatial_index=False), nullable=False
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SourcePOI(Base):
    """Upstream state, GA hydrology, and OSM POI staging table."""

    __tablename__ = "source_pois"
    __table_args__ = (
        CheckConstraint(
            "source_provider IN ("
            "'vic_foi','vic_recweb_hut','vic_recweb_site','vic_geomark',"
            "'nsw_poi','nsw_trig','nsw_hut',"
            "'act_heritage_hut','act_feature_name','act_bbq','act_shelter','act_trig',"
            "'tas_hut','tas_nomenclature','tas_infra','tas_picnic','tas_trig',"
            "'qld_tourist','qld_survey',"
            "'wa_ratis_site','wa_ratis_facility','wa_fire_tower','wa_geodetic',"
            "'sa_parks','sa_survey_mark',"
            "'nt_placename','nt_survey_mark',"
            "'ga_hydro_pt','osm')",
            name="source_provider_valid",
        ),
        CheckConstraint(
            "source_license IN ('CC-BY-4.0','CC-BY-3.0-AU','ODbL')", name="source_license_valid"
        ),
        CheckConstraint(
            "poi_type IN ('campground','hut','trig','lookout','picnic','water_point',"
            "'gate','trailhead','fuel')",
            name="poi_type_valid",
        ),
        CheckConstraint(
            "state IN ('VIC','NSW','ACT','TAS','QLD','WA','SA','NT')", name="state_valid"
        ),
        UniqueConstraint("source_provider", "source_feature_id", name="uq_source_pois_feature"),
        Index("ix_source_pois_geom", "geom", postgresql_using="gist"),
        Index("ix_source_pois_poi_type", "poi_type"),
        Index("ix_source_pois_state", "state"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_provider: Mapped[str] = mapped_column(Text, nullable=False)
    source_license: Mapped[str] = mapped_column(Text, nullable=False)
    source_feature_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    poi_type: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str | None] = mapped_column(Text, nullable=True)
    elev_m: Mapped[float | None] = mapped_column(Double, nullable=True)
    raw_props: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    geom: Mapped[Any] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=False
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SourceCAPADPark(Base):
    """Upstream DCCEEW CAPAD 2025 protected area polygons (CC BY 4.0)."""

    __tablename__ = "source_capad_parks"
    __table_args__ = (
        Index("ix_source_capad_parks_geom", "geom", postgresql_using="gist"),
        Index("ix_source_capad_parks_state_name", "state", "name"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_feature_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    park_type: Mapped[str] = mapped_column(Text, nullable=False)
    type_abbr: Mapped[str | None] = mapped_column(Text, nullable=True)
    iucn: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    geom: Mapped[Any] = mapped_column(
        Geometry("MULTIPOLYGON", srid=4326, spatial_index=False), nullable=False
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# =============================================================================
# Tier B — Canonical Conflated Network & Reference Tables (5 Tables)
# =============================================================================


class CAPADPark(Base):
    """Canonical protected area polygons for tenure attribution and park alert joins."""

    __tablename__ = "capad_parks"
    __table_args__ = (
        CheckConstraint(
            "state IN ('VIC','NSW','ACT','TAS','QLD','WA','SA','NT')", name="state_valid"
        ),
        Index("ix_capad_parks_geom", "geom", postgresql_using="gist"),
        Index("ix_capad_parks_state_lower_name", "state", text("lower(name)")),
        Index(
            "ix_capad_parks_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    capad_external_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    park_type: Mapped[str] = mapped_column(Text, nullable=False)
    type_abbr: Mapped[str | None] = mapped_column(Text, nullable=True)
    iucn: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    area_ha: Mapped[float | None] = mapped_column(Double, nullable=True)
    geom: Mapped[Any] = mapped_column(
        Geometry("MULTIPOLYGON", srid=4326, spatial_index=False), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Track(Base):
    """Logical named trail grouping contiguous canonical 50-100 m track segments."""

    __tablename__ = "tracks"
    __table_args__ = (
        CheckConstraint(
            "state ~ '^(VIC|NSW|ACT|TAS|QLD|WA|SA|NT)(/(VIC|NSW|ACT|TAS|QLD|WA|SA|NT))*$'",
            name="state_valid",
        ),
        CheckConstraint(
            "primary_source IN ('osm','vic','nsw','act','tas','qld','wa','sa','nt')",
            name="primary_source_valid",
        ),
        CheckConstraint(
            "trail_class IN ('4wd_track','fire_trail','management_trail',"
            "'unsealed_road','walking_track')",
            name="trail_class_valid",
        ),
        CheckConstraint(
            "effective_grade IN ('Easy','Medium','Hard','Extreme')",
            name="effective_grade_valid",
        ),
        Index("ix_tracks_bbox_geom", "bbox_geom", postgresql_using="gist"),
        Index("ix_tracks_state_is_active", "state", "is_active"),
        Index("ix_tracks_lower_name", text("lower(name)")),
        Index("ix_tracks_alt_names", "alt_names", postgresql_using="gin"),
        Index(
            "ix_tracks_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        Index(
            "ix_tracks_search_text_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
    )

    track_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    alt_names: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    search_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    primary_source: Mapped[str] = mapped_column(Text, nullable=False)
    has_osm_source: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    trail_class: Mapped[str] = mapped_column(Text, nullable=False)
    effective_grade: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_vehicular: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    total_length_m: Mapped[float] = mapped_column(
        Double, nullable=False, server_default=text("0")
    )
    elev_min_m: Mapped[float | None] = mapped_column(Double, nullable=True)
    elev_max_m: Mapped[float | None] = mapped_column(Double, nullable=True)
    elev_gain_m: Mapped[float] = mapped_column(Double, nullable=False, server_default=text("0"))
    max_slope_deg: Mapped[float | None] = mapped_column(Double, nullable=True)
    waterway_crossing_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    primary_park_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("capad_parks.id", ondelete="SET NULL"), nullable=True
    )
    bbox_geom: Mapped[Any | None] = mapped_column(
        Geometry("POLYGON", srid=4326, spatial_index=False), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TrackSegment(Base):
    """Canonical 50-100 m conflated track segment with deterministic stable segment_id."""

    __tablename__ = "track_segments"
    __table_args__ = (
        CheckConstraint("length_m > 0", name="length_positive"),
        CheckConstraint("bearing_deg >= 0 AND bearing_deg < 360", name="bearing_range"),
        CheckConstraint(
            "state IN ('VIC','NSW','ACT','TAS','QLD','WA','SA','NT')", name="state_valid"
        ),
        CheckConstraint(
            "trail_class IN ('4wd_track','fire_trail','management_trail',"
            "'unsealed_road','walking_track')",
            name="trail_class_valid",
        ),
        CheckConstraint(
            "surface_class IN ('sealed','gravel_compacted','dirt_unsealed',"
            "'rough_rock_mud_sand','unknown')",
            name="surface_class_valid",
        ),
        CheckConstraint(
            "tracktype IN ('grade1','grade2','grade3','grade4','grade5')",
            name="tracktype_valid",
        ),
        CheckConstraint(
            "access_status IN ('public','seasonal','permit','private','restricted','closed')",
            name="access_status_valid",
        ),
        CheckConstraint(
            "dem_tier IN ('5m-lidar','srtm-1s','synthetic')",
            name="dem_tier_valid",
        ),
        CheckConstraint(
            "primary_source IN ('osm','vic','nsw','act','tas','qld','wa','sa','nt')",
            name="primary_source_valid",
        ),
        Index("ix_track_segments_geom", "geom", postgresql_using="gist"),
        Index(
            "ix_track_segments_geom_active",
            "geom",
            postgresql_using="gist",
            postgresql_where=text("is_active = true"),
        ),
        Index("ix_track_segments_geom_fingerprint", "geom_fingerprint"),
        Index("ix_track_segments_track_id_seq", "track_id", "segment_seq"),
        Index("ix_track_segments_start_node_key", "start_node_key"),
        Index("ix_track_segments_end_node_key", "end_node_key"),
        Index("ix_track_segments_capad_park_id", "capad_park_id"),
        Index("ix_track_segments_active_vehicular_state", "is_active", "is_vehicular", "state"),
    )

    segment_id: Mapped[str] = mapped_column(Text, primary_key=True)
    track_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("tracks.track_id", ondelete="SET NULL"), nullable=True
    )
    segment_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    geom_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    geom: Mapped[Any] = mapped_column(
        Geometry("LINESTRING", srid=4326, spatial_index=False), nullable=False
    )
    start_node_key: Mapped[str] = mapped_column(Text, nullable=False)
    end_node_key: Mapped[str] = mapped_column(Text, nullable=False)
    length_m: Mapped[float] = mapped_column(Double, nullable=False)
    bearing_deg: Mapped[float] = mapped_column(Double, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    trail_class: Mapped[str] = mapped_column(Text, nullable=False)
    is_vehicular: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    surface_class: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unknown'")
    )
    tracktype: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_4wd_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_4wd_recommended: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    access_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'public'")
    )
    has_gate_barrier: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_bridged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    elev_min_m: Mapped[float | None] = mapped_column(Double, nullable=True)
    elev_max_m: Mapped[float | None] = mapped_column(Double, nullable=True)
    elev_start_m: Mapped[float | None] = mapped_column(Double, nullable=True)
    elev_end_m: Mapped[float | None] = mapped_column(Double, nullable=True)
    elev_gain_m: Mapped[float] = mapped_column(Double, nullable=False, server_default=text("0"))
    elev_loss_m: Mapped[float] = mapped_column(Double, nullable=False, server_default=text("0"))
    max_slope_deg: Mapped[float | None] = mapped_column(Double, nullable=True)
    sustained_slope_deg: Mapped[float | None] = mapped_column(Double, nullable=True)
    is_steep_highlight: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_above_snowline: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    elevation_profile_m: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    dem_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    waterway_crossing_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    has_perennial_crossing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    capad_park_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("capad_parks.id", ondelete="SET NULL"), nullable=True
    )
    primary_source: Mapped[str] = mapped_column(Text, nullable=False)
    has_osm_source: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    source_refs: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_segment_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("track_segments.segment_id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SegmentReconciliationLog(Base):
    """Audit lineage of segment ID preservation, splits, merges, and soft-retirements."""

    __tablename__ = "segment_reconciliation_log"
    __table_args__ = (
        CheckConstraint(
            "reconcile_action IN ('created','exact_match','spatial_preserved',"
            "'split','merge','retired')",
            name="reconcile_action_valid",
        ),
        Index("ix_segment_reconciliation_log_import_run_id", "import_run_id"),
        Index("ix_segment_reconciliation_log_old_segment_id", "old_segment_id"),
        Index("ix_segment_reconciliation_log_new_segment_id", "new_segment_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    import_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reconcile_action: Mapped[str] = mapped_column(Text, nullable=False)
    old_segment_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("track_segments.segment_id", ondelete="RESTRICT"), nullable=True
    )
    new_segment_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("track_segments.segment_id", ondelete="RESTRICT"), nullable=True
    )
    overlap_ratio: Mapped[float | None] = mapped_column(Double, nullable=True)
    hausdorff_m: Mapped[float | None] = mapped_column(Double, nullable=True)
    heading_diff_deg: Mapped[float | None] = mapped_column(Double, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class POI(Base):
    """Canonical deduplicated backcountry POIs (camps, huts, trigs, lookouts, gates)."""

    __tablename__ = "pois"
    __table_args__ = (
        CheckConstraint(
            "poi_type IN ('campground','hut','trig','lookout','picnic','water_point',"
            "'gate','trailhead','fuel')",
            name="poi_type_valid",
        ),
        CheckConstraint(
            "state IN ('VIC','NSW','ACT','TAS','QLD','WA','SA','NT')", name="state_valid"
        ),
        CheckConstraint(
            "source_license IN ('CC-BY-4.0','CC-BY-3.0-AU','ODbL')", name="source_license_valid"
        ),
        Index("ix_pois_geom", "geom", postgresql_using="gist"),
        Index("ix_pois_type_state", "poi_type", "state"),
        Index("ix_pois_lower_name", text("lower(name)")),
        Index(
            "ix_pois_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        Index("ix_pois_nearest_segment_id", "nearest_segment_id"),
        Index(
            "ix_pois_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    poi_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    poi_type: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    elev_m: Mapped[float | None] = mapped_column(Double, nullable=True)
    access_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    nearest_segment_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("track_segments.segment_id", ondelete="SET NULL"), nullable=True
    )
    capad_park_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("capad_parks.id", ondelete="SET NULL"), nullable=True
    )
    primary_source: Mapped[str] = mapped_column(Text, nullable=False)
    source_license: Mapped[str] = mapped_column(Text, nullable=False)
    source_refs: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    geom: Mapped[Any] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# =============================================================================
# Tier C — Independent TrailForge Intelligence, Community & Live Tables (18 Tables)
# Strictly zero raw OSM geometries and zero OSM tag blobs.
# =============================================================================


class TrackSegmentGrade(Base):
    """Explainable Day-1 baseline and live crowdsourced/rainfall difficulty grades."""

    __tablename__ = "track_segment_grades"
    __table_args__ = (
        CheckConstraint(
            "baseline_grade IN ('Easy','Medium','Hard','Extreme')",
            name="baseline_grade_valid",
        ),
        CheckConstraint("baseline_score >= 0 AND baseline_score <= 100", name="baseline_score_rng"),
        CheckConstraint(
            "live_grade IN ('Easy','Medium','Hard','Extreme')", name="live_grade_valid"
        ),
        CheckConstraint("live_score >= 0 AND live_score <= 100", name="live_score_range"),
        CheckConstraint(
            "effective_grade IN ('Easy','Medium','Hard','Extreme')",
            name="effective_grade_valid",
        ),
        CheckConstraint(
            "confidence_level IN ('baseline_only','low','medium','high')",
            name="confidence_level_valid",
        ),
        CheckConstraint("confidence_score >= 0 AND confidence_score <= 1", name="conf_score_range"),
        CheckConstraint(
            "wet_risk_level IN ('dry','damp','wet','saturated')", name="wet_risk_level_valid"
        ),
        Index("ix_track_segment_grades_effective_grade", "effective_grade"),
        Index("ix_track_segment_grades_confidence_level", "confidence_level"),
        Index("ix_track_segment_grades_last_driven_at", text("last_driven_at DESC")),
        Index("ix_track_segment_grades_wet_risk_level", "wet_risk_level"),
        Index(
            "ix_track_segment_grades_hotspot",
            "segment_id",
            postgresql_where=text("is_obstacle_hotspot = true"),
        ),
    )

    segment_id: Mapped[str] = mapped_column(
        Text, ForeignKey("track_segments.segment_id", ondelete="RESTRICT"), primary_key=True
    )
    baseline_grade: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_score: Mapped[float] = mapped_column(Double, nullable=False)
    baseline_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    live_grade: Mapped[str | None] = mapped_column(Text, nullable=True)
    live_score: Mapped[float | None] = mapped_column(Double, nullable=True)
    live_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    effective_grade: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_level: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'baseline_only'")
    )
    confidence_score: Mapped[float] = mapped_column(
        Double, nullable=False, server_default=text("0.0")
    )
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_driven_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    avg_speed_kmh: Mapped[float | None] = mapped_column(Double, nullable=True)
    avg_duration_s: Mapped[float | None] = mapped_column(Double, nullable=True)
    vehicle_class_split: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    rainfall_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    wet_risk_level: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'dry'")
    )
    is_obstacle_hotspot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    baseline_computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    live_computed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @validates("live_breakdown")
    def _sync_hotspot_from_breakdown(
        self, _key: str, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            self.is_obstacle_hotspot = False
        elif isinstance(value, dict) and "is_obstacle_hotspot" in value:
            self.is_obstacle_hotspot = bool(value.get("is_obstacle_hotspot"))
        return value


class ClosureAlert(Base):
    """Normalized VIC & NSW seasonal closures, emergency incidents, and park alerts."""

    __tablename__ = "closures_alerts"
    __table_args__ = (
        CheckConstraint(
            "source_feed IN ('vic_mapshare_seasonal','vic_emergency','nsw_rfs',"
            "'nsw_npws','osm_access','act_esa','tas_alert','qld_des_alerts',"
            "'wa_emergency','sa_cfs_parks','nt_securent')",
            name="source_feed_valid",
        ),
        CheckConstraint(
            "alert_type IN ('seasonal_closure','road_closure','park_closure','bushfire',"
            "'flood','hazard_warning','private_gate')",
            name="alert_type_valid",
        ),
        CheckConstraint(
            "severity IN ('info','advisory','watch_and_act','closed','emergency')",
            name="severity_valid",
        ),
        CheckConstraint(
            "status IN ('active','scheduled','cleared','stale')", name="status_valid"
        ),
        CheckConstraint(
            "state ~ '^(VIC|NSW|ACT|TAS|QLD|WA|SA|NT)(/(VIC|NSW|ACT|TAS|QLD|WA|SA|NT))*$'",
            name="state_valid",
        ),
        UniqueConstraint("source_feed", "external_id", name="uq_closures_alerts_feed_ext"),
        Index("ix_closures_alerts_geom", "geom", postgresql_using="gist"),
        Index("ix_closures_alerts_status_state_type", "status", "state", "alert_type"),
        Index("ix_closures_alerts_capad_park_id", "capad_park_id"),
        Index("ix_closures_alerts_feed_last_seen", "source_feed", text("last_seen_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_feed: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    alert_type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    capad_park_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("capad_parks.id", ondelete="SET NULL"), nullable=True
    )
    geom: Mapped[Any | None] = mapped_column(
        Geometry("GEOMETRY", srid=4326, spatial_index=False), nullable=True
    )
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SegmentClosureLink(Base):
    """Many-to-many join linking canonical segments to active closures/incidents."""

    __tablename__ = "segment_closure_links"
    __table_args__ = (
        CheckConstraint(
            "link_method IN ('spatial_intersect','spatial_buffer',"
            "'capad_park_join','osm_access_flag')",
            name="link_method_valid",
        ),
        Index("ix_segment_closure_links_closure_id", "closure_id"),
    )

    segment_id: Mapped[str] = mapped_column(
        Text, ForeignKey("track_segments.segment_id", ondelete="RESTRICT"), primary_key=True
    )
    closure_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("closures_alerts.id", ondelete="CASCADE"), primary_key=True
    )
    link_method: Mapped[str] = mapped_column(Text, nullable=False)
    distance_m: Mapped[float] = mapped_column(Double, nullable=False, server_default=text("0"))
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FeedSyncStatus(Base):
    """Feed health, last-success timestamp, and staleness tracking for official feeds."""

    __tablename__ = "feed_sync_status"
    __table_args__ = (
        CheckConstraint(
            "last_status IN ('never_run','ok','degraded','failed')", name="last_status_valid"
        ),
    )

    feed_name: Mapped[str] = mapped_column(Text, primary_key=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    sync_interval_s: Mapped[int] = mapped_column(Integer, nullable=False)
    stale_after_s: Mapped[int] = mapped_column(Integer, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'never_run'")
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    records_synced: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class User(Base):
    """Optional self-hostable TrailForge user account."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin','member')", name="role_valid"),
        Index("uq_users_lower_email", text("lower(email)"), unique=True),
        Index("ix_users_created_at", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'member'"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invited_by_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ui_preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InstanceUiSettings(Base):
    """Singleton admin-configurable default UI preferences & visibility settings (Tier C)."""

    __tablename__ = "instance_ui_settings"
    __table_args__ = (CheckConstraint("id = 1", name="singleton_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    default_layer_order: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    default_layer_visibility: Mapped[dict[str, bool] | None] = mapped_column(JSONB, nullable=True)
    show_stack_telemetry_public: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InviteCode(Base):
    """Invite-only registration codes for public instances (Tier C — zero OSM data)."""

    __tablename__ = "invite_codes"
    __table_args__ = (
        CheckConstraint("role IN ('admin','member')", name="role_valid"),
        CheckConstraint("max_uses >= 1", name="max_uses_min"),
        CheckConstraint("use_count >= 0", name="use_count_nonneg"),
        UniqueConstraint("code", name="uq_invite_codes_code"),
        Index("ix_invite_codes_created_at", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'member'"))
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    @validates("code")
    def _normalize_code(self, _key: str, value: str) -> str:
        return value.strip().upper()


class UsageMetricHourly(Base):
    """Hourly aggregated API request, error, egress, and rate-limit metrics (Tier C)."""

    __tablename__ = "usage_metrics_hourly"

    bucket_hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    route_group: Mapped[str] = mapped_column(String(48), primary_key=True)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    bytes_out: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    rate_limited_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )


class VehicleProfile(Base):
    """User vehicle setup classified into normalized Vehicle Capability Classes."""

    __tablename__ = "vehicle_profiles"
    __table_args__ = (
        CheckConstraint(
            "drivetrain IN ('awd','4wd_part_time','4wd_full_time')", name="drivetrain_valid"
        ),
        CheckConstraint("lift_mm >= 0 AND lift_mm <= 300", name="lift_range"),
        CheckConstraint(
            "ground_clearance_mm >= 120 AND ground_clearance_mm <= 500",
            name="clearance_range",
        ),
        CheckConstraint("tire_class IN ('HT','AT','MT')", name="tire_class_valid"),
        CheckConstraint(
            "capability_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD')",
            name="capability_class_valid",
        ),
        Index("ix_vehicle_profiles_user_id", "user_id"),
        Index(
            "uq_vehicle_profiles_user_default",
            "user_id",
            unique=True,
            postgresql_where=text("is_default = true"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    drivetrain: Mapped[str] = mapped_column(Text, nullable=False)
    has_low_range: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    lift_mm: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    ground_clearance_mm: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("210")
    )
    tire_class: Mapped[str] = mapped_column(Text, nullable=False)
    front_locker: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    rear_locker: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    has_winch: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    has_snorkel: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    capability_class: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserFavorite(Base):
    """User-saved favorite tracks or canonical segments."""

    __tablename__ = "user_favorites"
    __table_args__ = (
        CheckConstraint(
            "track_id IS NOT NULL OR segment_id IS NOT NULL",
            name="track_or_segment_required",
        ),
        Index("ix_user_favorites_user_created", "user_id", text("created_at DESC")),
        Index(
            "uq_user_favorites_user_track_only",
            "user_id",
            "track_id",
            unique=True,
            postgresql_where=text("segment_id IS NULL"),
        ),
        Index(
            "uq_user_favorites_user_segment_only",
            "user_id",
            "segment_id",
            unique=True,
            postgresql_where=text("track_id IS NULL"),
        ),
        Index(
            "uq_user_favorites_user_track_segment",
            "user_id",
            "track_id",
            "segment_id",
            unique=True,
            postgresql_where=text("track_id IS NOT NULL AND segment_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    track_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("tracks.track_id", ondelete="RESTRICT"), nullable=True
    )
    segment_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("track_segments.segment_id", ondelete="RESTRICT"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserPin(Base):
    """Personal saved waypoints/camps/hazards with offline PWA idempotency key."""

    __tablename__ = "user_pins"
    __table_args__ = (
        CheckConstraint(
            "category IN ('waypoint','camp','hazard','photo','water','recovery')",
            name="category_valid",
        ),
        UniqueConstraint("user_id", "client_uuid", name="uq_user_pins_user_client_uuid"),
        Index("ix_user_pins_geom", "geom", postgresql_using="gist"),
        Index("ix_user_pins_user_created", "user_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    client_uuid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'waypoint'"))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    elev_m: Mapped[float | None] = mapped_column(Double, nullable=True)
    geom: Mapped[Any] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Trip(Base):
    """User's private trip logbook with mandatory >=250 m privacy buffer & visibility toggle."""

    __tablename__ = "trips"
    __table_args__ = (
        CheckConstraint(
            "vehicle_capability_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD')",
            name="vehicle_class_valid",
        ),
        CheckConstraint(
            "visibility IN ('private','community_anonymous')", name="visibility_valid"
        ),
        CheckConstraint(
            "privacy_trim_m >= 250.0 AND privacy_trim_m <= 5000.0", name="privacy_trim_range"
        ),
        CheckConstraint(
            "match_status IN ('pending','matched','partial','unmapped',"
            "'skipped_private','skipped_too_short','failed')",
            name="match_status_valid",
        ),
        UniqueConstraint("user_id", "client_uuid", name="uq_trips_user_client_uuid"),
        Index("ix_trips_contribution_key_id", "contribution_key_id"),
        Index("ix_trips_raw_geom", "raw_geom", postgresql_using="gist"),
        Index("ix_trips_trimmed_geom", "trimmed_geom", postgresql_using="gist"),
        Index("ix_trips_user_created", "user_id", text("created_at DESC")),
        Index("ix_trips_match_status", "match_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    client_uuid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    vehicle_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vehicle_profiles.id", ondelete="SET NULL"), nullable=True
    )
    vehicle_capability_class: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    visibility: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'private'"))
    privacy_trim_m: Mapped[float] = mapped_column(
        Double, nullable=False, server_default=text("250.0")
    )
    contribution_nonce: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    contribution_key_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    community_consent_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    raw_geom: Mapped[Any] = mapped_column(
        Geometry("LINESTRING", srid=4326, spatial_index=False), nullable=False, deferred=True
    )
    trimmed_geom: Mapped[Any | None] = mapped_column(
        Geometry("LINESTRING", srid=4326, spatial_index=False), nullable=True, deferred=True
    )
    raw_distance_m: Mapped[float] = mapped_column(Double, nullable=False, server_default=text("0"))
    trimmed_distance_m: Mapped[float] = mapped_column(
        Double, nullable=False, server_default=text("0")
    )
    duration_s: Mapped[float | None] = mapped_column(Double, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    match_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    matcher_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    avg_match_confidence: Mapped[float | None] = mapped_column(Double, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TelemetryObservation(Base):
    """Strictly anonymized segment traversal record (ZERO user_id, ZERO trip_id)."""

    __tablename__ = "telemetry_observations"
    __table_args__ = (
        CheckConstraint(
            "vehicle_capability_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD')",
            name="vehicle_class_valid",
        ),
        CheckConstraint("direction IN ('forward','reverse')", name="direction_valid"),
        CheckConstraint("traversal_duration_s > 0", name="duration_positive"),
        CheckConstraint("avg_speed_kmh >= 0 AND avg_speed_kmh <= 160", name="avg_speed_range"),
        CheckConstraint("min_speed_kmh >= 0 AND min_speed_kmh <= 160", name="min_speed_range"),
        CheckConstraint("roughness_rms >= 0", name="roughness_rms_nonneg"),
        CheckConstraint("roughness_p95 >= 0", name="roughness_p95_nonneg"),
        CheckConstraint(
            "match_confidence >= 0 AND match_confidence <= 1", name="match_confidence_range"
        ),
        CheckConstraint("matched_point_count >= 1", name="matched_point_count_min"),
        Index(
            "ix_telemetry_observations_segment_observed",
            "segment_id",
            text("observed_at DESC"),
        ),
        Index("ix_telemetry_observations_contribution_hash", "contribution_hash"),
        Index(
            "ix_telemetry_observations_segment_vehicle",
            "segment_id",
            "vehicle_capability_class",
        ),
        Index(
            "ix_telemetry_observations_slow_hotspot_geom",
            "slow_hotspot_geom",
            postgresql_using="gist",
            postgresql_where=text("slow_hotspot_geom IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    segment_id: Mapped[str] = mapped_column(
        Text, ForeignKey("track_segments.segment_id", ondelete="RESTRICT"), nullable=False
    )
    contribution_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    vehicle_capability_class: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_date: Mapped[date] = mapped_column(Date, nullable=False)
    traversal_duration_s: Mapped[float] = mapped_column(Double, nullable=False)
    avg_speed_kmh: Mapped[float] = mapped_column(Double, nullable=False)
    min_speed_kmh: Mapped[float | None] = mapped_column(Double, nullable=True)
    roughness_rms: Mapped[float | None] = mapped_column(Double, nullable=True)
    roughness_p95: Mapped[float | None] = mapped_column(Double, nullable=True)
    match_confidence: Mapped[float] = mapped_column(Double, nullable=False)
    matched_point_count: Mapped[int] = mapped_column(Integer, nullable=False)
    slow_hotspot_geom: Mapped[Any | None] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=True
    )
    rainfall_72h_mm: Mapped[float | None] = mapped_column(Double, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UnmappedTrackCandidate(Base):
    """Anonymized off-network trace portion stored separately from canonical segments."""

    __tablename__ = "unmapped_track_candidates"
    __table_args__ = (
        CheckConstraint(
            "vehicle_capability_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD')",
            name="vehicle_class_valid",
        ),
        CheckConstraint("length_m > 0", name="length_positive"),
        CheckConstraint(
            "status IN ('pending_review','corroborated','promoted','rejected')",
            name="status_valid",
        ),
        Index("ix_unmapped_track_candidates_geom", "geom", postgresql_using="gist"),
        Index("ix_unmapped_track_candidates_contribution_hash", "contribution_hash"),
        Index("ix_unmapped_track_candidates_status_cluster", "status", "cluster_key"),
        Index(
            "ix_unmapped_track_candidates_public_recent",
            text("created_at DESC"),
            text("id DESC"),
            postgresql_where=text("status <> 'rejected'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    contribution_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    vehicle_capability_class: Mapped[str] = mapped_column(Text, nullable=False)
    geom: Mapped[Any] = mapped_column(
        Geometry("LINESTRING", srid=4326, spatial_index=False), nullable=False
    )
    length_m: Mapped[float] = mapped_column(Double, nullable=False)
    observed_date: Mapped[date] = mapped_column(Date, nullable=False)
    cluster_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_cell_lon: Mapped[float] = mapped_column(
        Double, Computed(
            "ST_X(ST_SnapToGrid(ST_LineInterpolatePoint(geom, 0.5), 0.02))", persisted=True
        ),
        nullable=False,
    )
    public_cell_lat: Mapped[float] = mapped_column(
        Double, Computed(
            "ST_Y(ST_SnapToGrid(ST_LineInterpolatePoint(geom, 0.5), 0.02))", persisted=True
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending_review'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConditionReport(Base):
    """Structured community hazard, bog hole, gate, and track-clear reports."""

    __tablename__ = "condition_reports"
    __table_args__ = (
        CheckConstraint(
            "report_type IN ('fallen_tree','bog_hole','washout','deep_water',"
            "'locked_gate','private_property','snow_ice','track_clear')",
            name="report_type_valid",
        ),
        CheckConstraint("severity IN ('info','caution','impassable')", name="severity_valid"),
        CheckConstraint("status IN ('active','resolved','stale','hidden')", name="status_valid"),
        CheckConstraint(
            "vehicle_capability_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD')",
            name="vehicle_class_valid",
        ),
        CheckConstraint("decay_score >= 0 AND decay_score <= 1", name="decay_score_range"),
        UniqueConstraint("user_id", "client_uuid", name="uq_condition_reports_user_client_uuid"),
        Index("ix_condition_reports_geom", "geom", postgresql_using="gist"),
        Index(
            "ix_condition_reports_segment_status_observed",
            "segment_id",
            "status",
            text("observed_at DESC"),
        ),
        Index("ix_condition_reports_type_status", "report_type", "status"),
        Index("ix_condition_reports_user_id", "user_id"),
        Index(
            "ix_condition_reports_track_status_created",
            "track_id",
            "status",
            text("created_at DESC"),
        ),
        Index(
            "ix_condition_reports_photo_url",
            "photo_url",
            postgresql_where=text("photo_url IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    client_uuid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    segment_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("track_segments.segment_id", ondelete="RESTRICT"), nullable=True
    )
    track_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("tracks.track_id", ondelete="SET NULL"), nullable=True
    )
    report_type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'caution'"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    vehicle_capability_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    geom: Mapped[Any] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=False
    )
    confirm_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    resolve_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    eligible_resolve_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    decay_score: Mapped[float] = mapped_column(Double, nullable=False, server_default=text("1.0"))
    osm_edit_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConditionReportVote(Base):
    """Per-user confirm / resolved / inaccurate vote on a community condition report."""

    __tablename__ = "condition_report_votes"
    __table_args__ = (
        CheckConstraint("vote_type IN ('confirm','resolved','inaccurate')", name="vote_type_valid"),
        UniqueConstraint("report_id", "user_id", name="uq_condition_report_votes_report_user"),
        Index("ix_condition_report_votes_report_type", "report_id", "vote_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("condition_reports.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    vote_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RainfallSnapshot(Base):
    """Regional 0.15-degree Open-Meteo precipitation snapshots for 72 h wet-risk context."""

    __tablename__ = "rainfall_snapshots"
    __table_args__ = (
        CheckConstraint("rain_24h_mm >= 0", name="rain_24h_nonneg"),
        CheckConstraint("rain_72h_mm >= 0", name="rain_72h_nonneg"),
        CheckConstraint("rain_7d_mm >= 0", name="rain_7d_nonneg"),
        CheckConstraint(
            "wet_risk_level IN ('dry','damp','wet','saturated')", name="wet_risk_level_valid"
        ),
        UniqueConstraint("grid_cell_id", "fetched_at", name="uq_rainfall_snapshots_cell_fetched"),
        Index("ix_rainfall_snapshots_centroid_geom", "centroid_geom", postgresql_using="gist"),
        Index(
            "ix_rainfall_snapshots_cell_fetched",
            "grid_cell_id",
            text("fetched_at DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    grid_cell_id: Mapped[str] = mapped_column(Text, nullable=False)
    centroid_geom: Mapped[Any] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=False
    )
    source_provider: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'open-meteo'")
    )
    rain_24h_mm: Mapped[float] = mapped_column(Double, nullable=False, server_default=text("0"))
    rain_72h_mm: Mapped[float] = mapped_column(Double, nullable=False, server_default=text("0"))
    rain_7d_mm: Mapped[float | None] = mapped_column(Double, nullable=True)
    wet_risk_level: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OfflinePack(Base):
    """Regional downloadable offline PMTiles + metadata snapshot bundles."""

    __tablename__ = "offline_packs"
    __table_args__ = (
        CheckConstraint("region_type IN ('tactical','state','custom')", name="region_type_valid"),
        CheckConstraint("status IN ('building','ready','failed')", name="status_valid"),
        Index("ix_offline_packs_bbox_geom", "bbox_geom", postgresql_using="gist"),
        Index("ix_offline_packs_status_region_type", "status", "region_type"),
    )

    pack_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    region_type: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    bbox_geom: Mapped[Any] = mapped_column(
        Geometry("POLYGON", srid=4326, spatial_index=False), nullable=False
    )
    utm_zones: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    vector_pmtiles_path: Mapped[str] = mapped_column(Text, nullable=False)
    terrain_pmtiles_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_json_path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attributions: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    community_telemetry_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    community_telemetry_revision: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'building'"))
    built_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OfflinePackTelemetryRevision(Base):
    """Latest community withdrawal affecting a pack region, including unbuilt regions."""

    __tablename__ = "offline_pack_telemetry_revisions"

    region_id: Mapped[str] = mapped_column(Text, primary_key=True)
    revision: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class PlannedRoute(Base):
    """User-saved multi-track planned route (Tier C — zero OSM geometries or raw OSM tags)."""

    __tablename__ = "planned_routes"
    __table_args__ = (
        CheckConstraint(
            "vehicle_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD')",
            name="vehicle_class_valid",
        ),
        Index("ix_planned_routes_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    track_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    waypoints_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    vehicle_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "Base",
    "CAPADPark",
    "ClosureAlert",
    "ConditionReport",
    "ConditionReportVote",
    "FeedSyncStatus",
    "InstanceUiSettings",
    "InviteCode",
    "OAuthAuthorizationCode",
    "OAuthClient",
    "OAuthToken",
    "OfflinePack",
    "OfflinePackTelemetryRevision",
    "POI",
    "PersonalAccessToken",
    "PlannedRoute",
    "RainfallSnapshot",
    "SegmentClosureLink",
    "SegmentReconciliationLog",
    "SourceCAPADPark",
    "SourcePOI",
    "SourceTrackAUState",
    "SourceTrackNSW",
    "SourceTrackOSM",
    "SourceTrackVIC",
    "SourceWaterway",
    "TelemetryObservation",
    "Track",
    "TrackSegment",
    "TrackSegmentGrade",
    "Trip",
    "UnmappedTrackCandidate",
    "UsageMetricHourly",
    "User",
    "UserFavorite",
    "UserPin",
    "VehicleProfile",
]
