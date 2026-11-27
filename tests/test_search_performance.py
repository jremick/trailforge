"""Offline unit & PostGIS integration tests for backend search performance optimizations."""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import trailforge.api.tracks as tracks_api
from trailforge.api.geocoder import GeocodedPlace
from trailforge.api.main import create_app
from trailforge.api.tracks import (
    SearchResultItem,
    _merge_search_results,
    reset_search_cache,
    search_network,
)
from trailforge.config import get_settings
from trailforge.db.base import Base
from trailforge.db.models import Track, TrackSegment
from trailforge.db.session import get_db
from trailforge.grading.live import recompute_live_grades, sync_track_effective_grades
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.pipeline import build_track_search_text, ingest_corridor_dataset

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MIGRATION_0008_PATH = (
    REPO_ROOT / "migrations" / "versions" / "0008_perf_and_security_indexes.py"
)
MIGRATION_0009_PATH = (
    REPO_ROOT / "migrations" / "versions" / "0009_search_trigram_and_track_grade.py"
)


def _compile_pg(stmt: Any) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


class _SpyResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _SpySearchSession:
    """Minimal SQLAlchemy-compatible fake Session that records compiled PG SQL statements."""

    def __init__(
        self,
        *,
        track_rows: list[Any] | None = None,
        fallback_rows: list[Any] | None = None,
        poi_rows: list[Any] | None = None,
        park_rows: list[Any] | None = None,
        engine_sentinel: object | None = None,
    ) -> None:
        self.track_rows = track_rows or []
        self.fallback_rows = fallback_rows or []
        self.poi_rows = poi_rows or []
        self.park_rows = park_rows or []
        self.executed_sql: list[str] = []
        self._engine_sentinel = engine_sentinel or object()

    def get_bind(self) -> object:
        return self._engine_sentinel

    def execute(self, stmt: Any, *_args: Any, **_kwargs: Any) -> _SpyResult:
        sql = _compile_pg(stmt)
        self.executed_sql.append(sql)
        lowered = sql.lower()
        if "from track_segments" in lowered:
            return _SpyResult(self.fallback_rows)
        if "from tracks" in lowered:
            return _SpyResult(self.track_rows)
        if "from pois" in lowered:
            return _SpyResult(self.poi_rows)
        if "from capad_parks" in lowered:
            return _SpyResult(self.park_rows)
        return _SpyResult([])


def _track_row(
    track_id: str,
    name: str,
    *,
    state: str = "VIC",
    trail_class: str = "4wd_track",
    effective_grade: str | None = "Hard",
    bbox: tuple[float, float, float, float] = (148.10, -36.66, 148.14, -36.58),
) -> Any:
    xmin, ymin, xmax, ymax = bbox
    row_tuple = (track_id, name, state, trail_class, effective_grade, xmin, ymin, xmax, ymax)

    class _Row(tuple):
        pass

    row = _Row(row_tuple)
    row.track_id = track_id
    row.name = name
    row.state = state
    row.trail_class = trail_class
    row.effective_grade = effective_grade
    row.xmin = xmin
    row.ymin = ymin
    row.xmax = xmax
    row.ymax = ymax
    return row


# -----------------------------------------------------------------------------
# Pure Unit Tests (100% offline, zero DB required)
# -----------------------------------------------------------------------------


def test_migration_0009_structure_and_orm_metadata() -> None:
    assert MIGRATION_0008_PATH.is_file()
    assert MIGRATION_0009_PATH.is_file()
    spec = importlib.util.spec_from_file_location("migration_0009", MIGRATION_0009_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.revision == "0009_search_trigram_track_grade"
    assert mod.down_revision == "0008_perf_and_security_indexes"
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)

    src_0008 = MIGRATION_0008_PATH.read_text(encoding="utf-8")
    src_0009 = MIGRATION_0009_PATH.read_text(encoding="utf-8")
    combined_src = f"{src_0008}\n{src_0009}"
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in src_0009
    assert "ix_tracks_search_text_trgm" in src_0009
    for idx_name in (
        "ix_tracks_name_trgm",
        "ix_tracks_search_text_trgm",
        "ix_pois_name_trgm",
        "ix_capad_parks_name_trgm",
    ):
        assert idx_name in combined_src
    assert "gin_trgm_ops" in src_0009
    assert "effective_grade" in src_0009
    assert "search_text" in src_0009

    # ORM metadata must mirror migrations 0008 + 0009 cleanly for `alembic check`
    tracks_table = Base.metadata.tables["tracks"]
    pois_table = Base.metadata.tables["pois"]
    parks_table = Base.metadata.tables["capad_parks"]
    assert {"effective_grade", "search_text"}.issubset(tracks_table.c.keys())

    all_index_names = {
        idx.name
        for tbl in (tracks_table, pois_table, parks_table)
        for idx in tbl.indexes
    }
    assert {
        "ix_tracks_name_trgm",
        "ix_tracks_search_text_trgm",
        "ix_pois_name_trgm",
        "ix_capad_parks_name_trgm",
    }.issubset(all_index_names)


def test_build_track_search_text_deduplicates_name_and_aliases() -> None:
    assert (
        build_track_search_text(
            "Davies Plain Track Lower",
            ["DAVIES PLAIN TRACK", "davies plain track lower", "  ", "Davies Plain Tk"],
        )
        == "Davies Plain Track Lower DAVIES PLAIN TRACK Davies Plain Tk"
    )
    assert build_track_search_text("Omeo Hwy", []) == "Omeo Hwy"
    assert build_track_search_text("  ", []) is None


def test_merge_search_results_budgets_slots_and_ranks_exact_prefix() -> None:
    # 20 matching tracks must NOT silently crowd out matching parks & POIs
    many_tracks = [
        SearchResultItem(
            kind="track",
            id=f"trk_{i}",
            name=f"Alpine Spur Track {i}",
            state="VIC",
            grade="Medium",
        )
        for i in range(20)
    ]
    parks = [
        SearchResultItem(kind="park", id="1", name="Alpine National Park", state="VIC"),
        SearchResultItem(kind="park", id="2", name="Alpine Reserve", state="VIC"),
    ]
    pois = [
        SearchResultItem(kind="poi", id="10", name="Alpine Hut", state="VIC"),
        SearchResultItem(kind="poi", id="11", name="Alpine Lookout", state="VIC"),
    ]

    merged = _merge_search_results(many_tracks, pois, parks, term="Alpine", limit=20)
    assert len(merged) == 20
    kinds = [item.kind for item in merged]
    assert kinds[0] == "track"
    assert kinds.count("park") >= 2
    assert kinds.count("poi") >= 2
    assert kinds.count("track") == 16

    # Exact match on a park must rank #1 ahead of substring tracks
    sub_tracks = [
        SearchResultItem(kind="track", id="t1", name="Great Alpine Track", state="VIC"),
        SearchResultItem(kind="track", id="t2", name="Upper Alpine Link", state="VIC"),
    ]
    exact_merged = _merge_search_results(
        sub_tracks,
        pois,
        parks,
        term="Alpine National Park",
        limit=5,
    )
    assert exact_merged[0].kind == "park"
    assert exact_merged[0].name == "Alpine National Park"

    # Small limit=2 still surfaces prefix park over substring tracks
    small_merged = _merge_search_results(sub_tracks, [], parks[:1], term="Alpine", limit=2)
    assert len(small_merged) == 2
    assert small_merged[0].kind == "park"


def test_search_network_uses_cached_effective_grade_without_segment_join() -> None:
    reset_search_cache()
    spy = _SpySearchSession(
        track_rows=[
            _track_row("trk_1", "Davies Plain Track Lower", effective_grade="Extreme"),
            _track_row("trk_2", "Davies Plain Ridge", effective_grade="Hard"),
        ],
        poi_rows=[
            (11, "Davies Plain Hut", "VIC", "hut", 148.1205, -36.6123),
        ],
        park_rows=[
            (7, "Alpine National Park", "VIC", "National Park", 148.0, -36.7, 148.4, -36.5),
        ],
    )

    resp = search_network(q="Davies", limit=20, db=spy)  # type: ignore[arg-type]

    assert resp.query == "Davies"
    assert resp.total == 4
    # Exactly 3 queries: tracks, pois, capad_parks — ZERO track_segments/grades fallback!
    assert len(spy.executed_sql) == 3
    joined_sql = "\n".join(spy.executed_sql).lower()
    assert "track_segments" not in joined_sql
    assert "track_segment_grades" not in joined_sql
    assert "st_pointonsurface" not in joined_sql
    assert "st_centroid" not in joined_sql
    assert "array_to_string" not in joined_sql
    assert "tracks.search_text" in joined_sql
    assert "tracks.effective_grade" in joined_sql

    # Verify capped subquery limits for POIs (<=5) and parks (<=4)
    poi_sql = next(s for s in spy.executed_sql if "FROM pois" in s)
    park_sql = next(s for s in spy.executed_sql if "FROM capad_parks" in s)
    assert "LIMIT 5" in poi_sql
    assert "LIMIT 4" in park_sql

    # Verify computed bbox midpoint coordinates for both track and park
    trk_hit = next(r for r in resp.results if r.id == "trk_1")
    assert trk_hit.grade == "Extreme"
    assert trk_hit.lon == pytest.approx(148.12)
    assert trk_hit.lat == pytest.approx(-36.62)
    assert trk_hit.bbox == [148.1, -36.66, 148.14, -36.58]

    park_hit = next(r for r in resp.results if r.kind == "park")
    assert park_hit.lon == pytest.approx(148.2)
    assert park_hit.lat == pytest.approx(-36.6)
    assert park_hit.bbox == [148.0, -36.7, 148.4, -36.5]


def test_search_network_falls_back_only_for_null_effective_grade_and_multi_token() -> None:
    reset_search_cache()
    spy = _SpySearchSession(
        track_rows=[
            _track_row("trk_cached", "Davies Plain Track Lower", effective_grade="Extreme"),
            _track_row("trk_legacy", "Davies Plain Legacy Spur", effective_grade=None),
        ],
        fallback_rows=[("trk_legacy", 2)],
    )

    resp = search_network(q="davies plain", limit=10, db=spy)  # type: ignore[arg-type]

    assert resp.total == 2
    by_id = {r.id: r for r in resp.results}
    assert by_id["trk_cached"].grade == "Extreme"
    assert by_id["trk_legacy"].grade == "Medium"

    # Fallback query must target ONLY trk_legacy, not trk_cached
    seg_sql = next(s for s in spy.executed_sql if "FROM track_segments" in s)
    assert "'trk_legacy'" in seg_sql
    assert "'trk_cached'" not in seg_sql

    # Multi-token query must match each token against search_text / name without array_to_string
    track_sql = spy.executed_sql[0].lower()
    assert "%davies%" in track_sql
    assert "%plain%" in track_sql
    assert "array_to_string" not in track_sql


def test_search_network_lru_ttl_cache_and_geocoder_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_search_cache()
    clock = [1000.0]
    monkeypatch.setattr(tracks_api.time, "monotonic", lambda: clock[0])

    shared_engine = object()
    spy1 = _SpySearchSession(
        track_rows=[_track_row("trk_1", "Davies Plain Track Lower", effective_grade="Extreme")],
        engine_sentinel=shared_engine,
    )
    first = search_network(q="Davies", limit=20, db=spy1)  # type: ignore[arg-type]
    assert len(spy1.executed_sql) == 3

    # Mutating returned response must not poison cached value
    first.results[0].name = "MUTATED"

    # Case & whitespace variant hits cache with ZERO DB queries
    spy2 = _SpySearchSession(engine_sentinel=shared_engine)
    second = search_network(q="  davies  ", limit=20, db=spy2)  # type: ignore[arg-type]
    assert len(spy2.executed_sql) == 0
    assert second.results[0].name == "Davies Plain Track Lower"

    # Advancing monotonic clock past TTL expires cache entry
    clock[0] += tracks_api._SEARCH_CACHE_TTL_SECONDS + 1.0
    spy3 = _SpySearchSession(
        track_rows=[_track_row("trk_1", "Davies Plain Track Lower", effective_grade="Hard")],
        engine_sentinel=shared_engine,
    )
    third = search_network(q="Davies", limit=20, db=spy3)  # type: ignore[arg-type]
    assert len(spy3.executed_sql) == 3
    assert third.results[0].grade == "Hard"

    # reset_search_cache() forces fresh query immediately
    reset_search_cache()
    spy4 = _SpySearchSession(
        track_rows=[_track_row("trk_1", "Davies Plain Track Lower", effective_grade="Medium")],
        engine_sentinel=shared_engine,
    )
    fourth = search_network(q="Davies", limit=20, db=spy4)  # type: ignore[arg-type]
    assert len(spy4.executed_sql) == 3
    assert fourth.results[0].grade == "Medium"

    # LRU eviction keeps recently used entry and evicts oldest entry when capacity exceeded
    reset_search_cache()
    monkeypatch.setattr(tracks_api, "_SEARCH_CACHE_MAX_ENTRIES", 2)
    search_network(q="alpha", limit=5, db=_SpySearchSession(engine_sentinel=shared_engine))  # type: ignore[arg-type]
    search_network(q="beta", limit=5, db=_SpySearchSession(engine_sentinel=shared_engine))  # type: ignore[arg-type]
    # Touch "alpha" so "beta" becomes least-recently used
    spy_touch = _SpySearchSession(engine_sentinel=shared_engine)
    search_network(q="alpha", limit=5, db=spy_touch)  # type: ignore[arg-type]
    assert len(spy_touch.executed_sql) == 0
    # Insert "gamma" -> evicts "beta", keeps "alpha"
    search_network(q="gamma", limit=5, db=_SpySearchSession(engine_sentinel=shared_engine))  # type: ignore[arg-type]
    spy_alpha = _SpySearchSession(engine_sentinel=shared_engine)
    search_network(q="alpha", limit=5, db=spy_alpha)  # type: ignore[arg-type]
    assert len(spy_alpha.executed_sql) == 0
    spy_beta = _SpySearchSession(engine_sentinel=shared_engine)
    search_network(q="beta", limit=5, db=spy_beta)  # type: ignore[arg-type]
    assert len(spy_beta.executed_sql) == 3

    # Whitespace-only query short-circuits with zero DB calls
    spy_blank = _SpySearchSession(engine_sentinel=shared_engine)
    blank = search_network(q="   ", limit=10, db=spy_blank)  # type: ignore[arg-type]
    assert blank.total == 0
    assert len(spy_blank.executed_sql) == 0

    # Nominatim geocoder fallback runs ONLY for empty local matches with len >= 3 & enabled
    reset_search_cache()
    geocode_calls: list[str] = []

    def _fake_geocode(term: str, limit: int = 5) -> list[GeocodedPlace]:
        geocode_calls.append(term)
        return [
            GeocodedPlace(
                id="place-bright",
                name="Bright",
                state="VIC",
                subtitle="Town, Victoria",
                category="town",
                lon=146.9608,
                lat=-36.7285,
                bbox=[146.93, -36.75, 146.99, -36.70],
            )
        ]

    monkeypatch.setattr(tracks_api, "geocode_au_places", _fake_geocode)
    monkeypatch.setenv("TRAILFORGE_ENABLE_GEOCODER", "true")
    get_settings.cache_clear()

    # 2-char term must NOT trigger geocoder even with 0 local results
    search_network(q="Br", limit=10, db=_SpySearchSession(engine_sentinel=shared_engine))  # type: ignore[arg-type]
    assert geocode_calls == []

    # Local hit must NOT trigger geocoder
    search_network(
        q="Davies",
        limit=10,
        db=_SpySearchSession(
            track_rows=[_track_row("trk_1", "Davies Plain Track Lower")],
            engine_sentinel=shared_engine,
        ),  # type: ignore[arg-type]
    )
    assert geocode_calls == []

    # 3+ char empty local match triggers geocoder
    geo_resp = search_network(
        q="Bright",
        limit=10,
        db=_SpySearchSession(engine_sentinel=shared_engine),  # type: ignore[arg-type]
    )
    assert geocode_calls == ["Bright"]
    assert geo_resp.total == 1
    assert geo_resp.results[0].kind == "place"
    assert geo_resp.results[0].name == "Bright"
    reset_search_cache()


# -----------------------------------------------------------------------------
# PostGIS Integration Tests (100% offline against isolated PostGIS test DB)
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded_search_engine(postgis_engine: Engine) -> Engine:
    """Ensure the synthetic Australian Alps corridor is seeded for search tests."""
    with Session(postgis_engine) as session:
        seg_count = session.execute(
            text("SELECT COUNT(*) FROM track_segments WHERE is_active = TRUE")
        ).scalar_one()
        if seg_count == 0:
            ingest_corridor_dataset(session, load_corridor_fixtures(FIXTURES_DIR))
            session.commit()
        else:
            sync_track_effective_grades(session)
            session.commit()
    return postgis_engine


@pytest.mark.integration
def test_postgis_trigram_indexes_alembic_check_and_trigger(postgis_engine: Engine) -> None:
    # 1. Verify pg_trgm extension + all 4 GIN gin_trgm_ops indexes exist
    with postgis_engine.connect() as conn:
        ext_ver = conn.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm'")
        ).scalar_one_or_none()
        assert ext_ver is not None

        idx_defs = dict(
            conn.execute(
                text(
                    """
                    SELECT indexname, indexdef
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND indexname IN (
                        'ix_tracks_name_trgm',
                        'ix_tracks_search_text_trgm',
                        'ix_pois_name_trgm',
                        'ix_capad_parks_name_trgm'
                      )
                    """
                )
            ).all()
        )
    assert len(idx_defs) == 4
    for name, idxdef in idx_defs.items():
        assert "USING gin" in idxdef, f"{name} must be a GIN index: {idxdef}"
        assert "gin_trgm_ops" in idxdef, f"{name} must use gin_trgm_ops: {idxdef}"

    # 2. Verify `alembic check` passes cleanly with zero drift against models.py
    # Pytest configures logging for this process, as in the shared migration fixture.
    alembic_cfg = AlembicConfig()
    alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    alembic_command.check(alembic_cfg)

    # 3. Verify trigger auto-populates/updates search_text and CHECK validates effective_grade
    tid = f"trk_trigger_{uuid.uuid4().hex[:10]}"
    with postgis_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(
                text(
                    """
                    INSERT INTO tracks (
                        track_id, name, alt_names, state, primary_source, trail_class
                    ) VALUES (
                        :tid, 'Wonnangatta Track', ARRAY['WONNANGATTA VALLEY TK'],
                        'VIC', 'osm', '4wd_track'
                    )
                    """
                ),
                {"tid": tid},
            )
            st1 = conn.execute(
                text("SELECT search_text FROM tracks WHERE track_id = :tid"),
                {"tid": tid},
            ).scalar_one()
            assert st1 == "Wonnangatta Track WONNANGATTA VALLEY TK"

            conn.execute(
                text(
                    "UPDATE tracks SET alt_names = ARRAY['HERNE SPUR'] WHERE track_id = :tid"
                ),
                {"tid": tid},
            )
            st2 = conn.execute(
                text("SELECT search_text FROM tracks WHERE track_id = :tid"),
                {"tid": tid},
            ).scalar_one()
            assert st2 == "Wonnangatta Track HERNE SPUR"

            sp = conn.begin_nested()
            with pytest.raises(IntegrityError):
                conn.execute(
                    text("UPDATE tracks SET effective_grade = 'Impossible' WHERE track_id = :tid"),
                    {"tid": tid},
                )
            sp.rollback()
        finally:
            trans.rollback()


@pytest.mark.integration
def test_postgis_search_uses_cached_grade_trigram_explain_and_sync(
    seeded_search_engine: Engine,
) -> None:
    reset_search_cache()

    with Session(seeded_search_engine) as session:
        tracks = session.execute(select(Track).where(Track.is_active.is_(True))).scalars().all()
        assert len(tracks) >= 3
        for trk in tracks:
            assert trk.effective_grade in ("Easy", "Medium", "Hard", "Extreme")
            assert trk.search_text is not None
            assert trk.name in trk.search_text

        davies = next(t for t in tracks if t.name == "Davies Plain Track Lower")
        assert davies.effective_grade == "Extreme"
        assert "DAVIES PLAIN TRACK" in (davies.search_text or "")

        # Verify PostgreSQL plans GIN trigram index scans across all 4 indexes
        # and zero ST_PointOnSurface. Insert 2,500 temporary tracks inside this
        # rolled-back transaction, flush GIN pending lists, and ANALYZE so planner
        # stats reflect realistic scale (even with default enable_seqscan = on!).
        session.execute(
            text(
                """
                INSERT INTO tracks (
                    track_id, name, alt_names, state, primary_source, trail_class, is_active
                )
                SELECT
                    'trk_bench_' || g,
                    'Synthetic High Country Spur ' || g,
                    ARRAY['SPUR ALIAS ' || g],
                    'VIC',
                    'osm',
                    '4wd_track',
                    TRUE
                FROM generate_series(1, 2500) AS g
                """
            )
        )
        session.execute(text("SELECT gin_clean_pending_list('ix_tracks_search_text_trgm')"))
        session.execute(text("SELECT gin_clean_pending_list('ix_tracks_name_trgm')"))
        session.execute(text("ANALYZE tracks"))

        track_search_plan = "\n".join(
            session.execute(
                text(
                    """
                    EXPLAIN
                    SELECT track_id, name, effective_grade
                    FROM tracks
                    WHERE is_active IS TRUE
                      AND search_text ILIKE '%davies%'
                    LIMIT 20
                    """
                )
            ).scalars().all()
        )
        assert "ix_tracks_search_text_trgm" in track_search_plan
        assert "Bitmap Index Scan" in track_search_plan

        track_name_plan = "\n".join(
            session.execute(
                text(
                    """
                    EXPLAIN
                    SELECT track_id, name
                    FROM tracks
                    WHERE name ILIKE '%davies%'
                    LIMIT 20
                    """
                )
            ).scalars().all()
        )
        assert "ix_tracks_name_trgm" in track_name_plan

        session.execute(text("SET LOCAL enable_seqscan = off"))
        poi_plan = "\n".join(
            session.execute(
                text("EXPLAIN SELECT id, name FROM pois WHERE name ILIKE '%davies%' LIMIT 5")
            ).scalars().all()
        )
        assert "ix_pois_name_trgm" in poi_plan

        park_plan = "\n".join(
            session.execute(
                text(
                    """
                    EXPLAIN
                    SELECT id, name,
                           (ST_XMin(geom) + ST_XMax(geom)) / 2.0 AS lon,
                           (ST_YMin(geom) + ST_YMax(geom)) / 2.0 AS lat
                    FROM capad_parks
                    WHERE name ILIKE '%alpine%'
                    LIMIT 4
                    """
                )
            ).scalars().all()
        )
        assert "ix_capad_parks_name_trgm" in park_plan
        assert "ST_PointOnSurface" not in park_plan
        session.rollback()

    # Exercise GET /api/search via FastAPI and inspect every executed SQL statement
    executed_sql: list[str] = []

    def _capture_sql(
        _conn: Any, _cursor: Any, statement: str, _params: Any, _ctx: Any, _execmany: Any
    ) -> None:
        executed_sql.append(statement)

    app = create_app()

    def _override_db():
        with Session(seeded_search_engine) as s:
            yield s

    app.dependency_overrides[get_db] = _override_db
    event.listen(seeded_search_engine, "before_cursor_execute", _capture_sql)
    try:
        with TestClient(app) as client:
            reset_search_cache()
            executed_sql.clear()
            r1 = client.get("/api/search", params={"q": "Davies"})
            assert r1.status_code == 200
            body1 = r1.json()
            assert body1["total"] >= 2
            davies_hit = next(
                item for item in body1["results"] if item["name"] == "Davies Plain Track Lower"
            )
            assert davies_hit["kind"] == "track"
            assert davies_hit["grade"] == "Extreme"
            assert davies_hit["bbox"] is not None and len(davies_hit["bbox"]) == 4
            assert davies_hit["lon"] == pytest.approx(
                (davies_hit["bbox"][0] + davies_hit["bbox"][2]) / 2.0, abs=1e-5
            )
            assert davies_hit["lat"] == pytest.approx(
                (davies_hit["bbox"][1] + davies_hit["bbox"][3]) / 2.0, abs=1e-5
            )
            assert any(
                item["kind"] == "poi" and item["name"] == "Davies Plain Hut"
                for item in body1["results"]
            )

            # ZERO segment joins, ZERO ST_PointOnSurface, ZERO array_to_string
            sql_blob = "\n".join(executed_sql).lower()
            assert "track_segments" not in sql_blob
            assert "track_segment_grades" not in sql_blob
            assert "st_pointonsurface" not in sql_blob
            assert "array_to_string" not in sql_blob

            # Repeated identical search hits in-process cache -> ZERO SQL executed
            executed_sql.clear()
            r1_cached = client.get("/api/search", params={"q": "davies"})
            assert r1_cached.status_code == 200
            assert r1_cached.json()["results"] == body1["results"]
            assert executed_sql == []

            # Alias search via search_text matches both Davies Plain tracks
            r_alias = client.get("/api/search", params={"q": "DAVIES PLAIN TRACK"})
            assert r_alias.status_code == 200
            alias_names = {item["name"] for item in r_alias.json()["results"]}
            assert {"Davies Plain Track Lower", "Davies Plain Ridge"}.issubset(alias_names)

            # CAPAD park search returns valid bbox and bbox midpoint lon/lat
            r_park = client.get("/api/search", params={"q": "Alpine"})
            assert r_park.status_code == 200
            park_hit = next(
                item for item in r_park.json()["results"] if item["name"] == "Alpine National Park"
            )
            assert park_hit["kind"] == "park"
            assert park_hit["bbox"] is not None and len(park_hit["bbox"]) == 4
            assert park_hit["lon"] == pytest.approx(
                (park_hit["bbox"][0] + park_hit["bbox"][2]) / 2.0, abs=1e-5
            )
            assert park_hit["lat"] == pytest.approx(
                (park_hit["bbox"][1] + park_hit["bbox"][3]) / 2.0, abs=1e-5
            )
    finally:
        event.remove(seeded_search_engine, "before_cursor_execute", _capture_sql)
        app.dependency_overrides.clear()
        reset_search_cache()

    # Verify recompute_live_grades keeps tracks.effective_grade synchronized
    with Session(seeded_search_engine) as session:
        access_track = session.execute(
            select(Track).where(Track.name == "Tom Groggin Access Rd")
        ).scalar_one()
        orig_grade = access_track.effective_grade
        access_seg_ids = list(
            session.execute(
                select(TrackSegment.segment_id).where(
                    TrackSegment.track_id == access_track.track_id,
                    TrackSegment.is_active.is_(True),
                )
            ).scalars().all()
        )
        # Clear cached grade temporarily, run recompute_live_grades for those segments,
        # and verify tracks.effective_grade is repopulated accurately.
        access_track.effective_grade = None
        session.flush()
        recompute_live_grades(
            session,
            now=datetime.now(UTC) - timedelta(hours=1),
            rain_override_72h_mm=0.0,
            rain_override_24h_mm=0.0,
            segment_ids=access_seg_ids,
        )
        reloaded = session.get(Track, access_track.track_id)
        assert reloaded is not None
        assert reloaded.effective_grade == orig_grade
        session.rollback()
