"""Offline unit and PostGIS integration tests for Slice 03 PMTiles Range & Track Inspector API."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from trailforge.api.main import create_app
from trailforge.api.tiles import get_tiles_dir
from trailforge.config import Settings
from trailforge.db.session import get_db
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset


@pytest.fixture
def tiles_client(tmp_path: Path) -> Generator[tuple[TestClient, Path], None, None]:
    app = create_app()
    app.dependency_overrides[get_tiles_dir] = lambda: tmp_path
    with TestClient(app) as client:
        yield client, tmp_path
    app.dependency_overrides.clear()


def test_tiles_dir_env_alias_support(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TILES_DIR", "/tmp/custom-tiles")
    cfg = Settings()
    assert cfg.tiles_dir == "/tmp/custom-tiles"
    assert cfg.trailforge_tiles_dir == "/tmp/custom-tiles"


def test_manifest_fallback_when_tiles_not_yet_built(
    tiles_client: tuple[TestClient, Path],
) -> None:
    client, _tiles_dir = tiles_client
    resp = client.get("/api/tiles/manifest.json")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-cache"
    body = resp.json()
    assert body["vector_url"] == "/api/tiles/trailforge_vector.pmtiles"
    assert body["terrain_url"] == "/api/tiles/trailforge_terrain.pmtiles"
    assert body["vector_available"] is False
    assert body["terrain_available"] is False
    assert body["snowline_bands_m"] == [800, 900, 1000, 1100, 1200, 1400, 1600]
    assert len(body["attributions"]) >= 5


def test_manifest_merges_disk_manifest_and_availability(
    tiles_client: tuple[TestClient, Path],
) -> None:
    client, tiles_dir = tiles_client
    (tiles_dir / "trailforge_vector.pmtiles").write_bytes(b"PMTiles" + b"\x00" * 64)
    (tiles_dir / "manifest.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-11-26T01:00:00Z",
                "bounds": [148.05, -36.68, 148.20, -36.55],
                "minzoom": 8,
                "maxzoom": 15,
                "layers": ["track_segments", "steep_sections", "contours", "snowline_bands"],
            }
        ),
        encoding="utf-8",
    )

    resp = client.get("/api/tiles/manifest.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["generated_at"] == "2026-11-26T01:00:00Z"
    assert body["vector_available"] is True
    assert body["terrain_available"] is False
    assert "track_segments" in body["layers"]


def test_pmtiles_range_200_206_304_416_404_and_head(
    tiles_client: tuple[TestClient, Path],
) -> None:
    client, tiles_dir = tiles_client
    payload = bytes(range(256))  # 256 deterministic bytes
    archive = tiles_dir / "trailforge_vector.pmtiles"
    archive.write_bytes(payload)

    # 1. Full GET -> 200 OK
    r_full = client.get("/api/tiles/trailforge_vector.pmtiles")
    assert r_full.status_code == 200
    assert r_full.content == payload
    assert r_full.headers["accept-ranges"] == "bytes"
    assert r_full.headers["content-length"] == "256"
    assert r_full.headers["content-type"] == "application/vnd.pmtiles"
    etag = r_full.headers["etag"]
    assert etag.startswith('"')

    # 2. HEAD -> 200 OK with empty body & matching headers
    r_head = client.head("/api/tiles/trailforge_vector.pmtiles")
    assert r_head.status_code == 200
    assert r_head.content == b""
    assert r_head.headers["content-length"] == "256"
    assert r_head.headers["accept-ranges"] == "bytes"
    assert r_head.headers["etag"] == etag

    # 3. Conditional ETag -> 304 Not Modified
    r_304 = client.get("/api/tiles/trailforge_vector.pmtiles", headers={"If-None-Match": etag})
    assert r_304.status_code == 304

    # 4. Exact byte Range -> 206 Partial Content
    r_206 = client.get(
        "/api/tiles/trailforge_vector.pmtiles", headers={"Range": "bytes=10-25"}
    )
    assert r_206.status_code == 206
    assert r_206.content == payload[10:26]
    assert r_206.headers["content-range"] == "bytes 10-25/256"
    assert r_206.headers["content-length"] == "16"

    # 5. Open-ended Range -> 206 Partial Content
    r_open = client.get(
        "/api/tiles/trailforge_vector.pmtiles", headers={"Range": "bytes=240-"}
    )
    assert r_open.status_code == 206
    assert r_open.content == payload[240:]
    assert r_open.headers["content-range"] == "bytes 240-255/256"

    # 6. Suffix Range -> 206 Partial Content
    r_suffix = client.get(
        "/api/tiles/trailforge_vector.pmtiles", headers={"Range": "bytes=-12"}
    )
    assert r_suffix.status_code == 206
    assert r_suffix.content == payload[-12:]
    assert r_suffix.headers["content-range"] == "bytes 244-255/256"

    # 7. Clamped upper bound Range -> 206 Partial Content (RFC 9110)
    r_clamp = client.get(
        "/api/tiles/trailforge_vector.pmtiles", headers={"Range": "bytes=250-9999"}
    )
    assert r_clamp.status_code == 206
    assert r_clamp.content == payload[250:]
    assert r_clamp.headers["content-range"] == "bytes 250-255/256"

    # 8. Out-of-bounds / malformed / multi-range -> 416 Range Not Satisfiable
    for bad_range in ("bytes=300-400", "bytes=20-10", "bytes=0-1,2-3", "items=0-10", "bytes=-0"):
        r_416 = client.get(
            "/api/tiles/trailforge_vector.pmtiles", headers={"Range": bad_range}
        )
        assert r_416.status_code == 416, f"Expected 416 for {bad_range}"
        assert r_416.headers["content-range"] == "bytes */256"

    # 9. Missing terrain archive & disallowed filenames -> 404 Not Found
    assert client.get("/api/tiles/trailforge_terrain.pmtiles").status_code == 404
    assert client.get("/api/tiles/secret.pmtiles").status_code == 404
    assert client.get("/api/tiles/..%2Fpyproject.toml").status_code == 404


@pytest.mark.integration
def test_enriched_track_inspector_and_search_api(
    postgis_engine: Engine,
    fixtures_dir: Path,
) -> None:
    """Verify Slice 03 enriched Track Inspector & unified search endpoints against PostGIS."""
    with postgis_engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    condition_report_votes, condition_reports,
                    telemetry_observations, unmapped_track_candidates,
                    segment_closure_links, closures_alerts,
                    segment_reconciliation_log, track_segment_grades,
                    pois, track_segments, tracks, capad_parks, users,
                    source_tracks_osm, source_tracks_vic, source_tracks_nsw,
                    source_waterways, source_pois, source_capad_parks
                RESTART IDENTITY CASCADE
                """
            )
        )

    with Session(postgis_engine) as session:
        dataset = load_corridor_fixtures(fixtures_dir, osm_filename="osm_tracks.geojson")
        ingest_corridor_dataset(session, dataset, commit=True)

        app = create_app()

        def _override_db() -> Generator[Session, None, None]:
            yield session

        app.dependency_overrides[get_db] = _override_db
        with TestClient(app) as client:
            tracks = client.get("/api/tracks").json()
            davies = next(t for t in tracks if t["name"] == "Davies Plain Track Lower")
            bridged = next(t for t in tracks if t["name"] == "Limestone Creek Bridged Link")
            assert davies["bbox"] is not None and len(davies["bbox"]) == 4
            assert davies["grade"] == "Extreme"
            assert davies["score"] is not None and davies["score"] >= 75.0

            # Enriched Track Inspector for Davies Plain Track Lower
            r_detail = client.get(f"/api/tracks/{davies['track_id']}")
            assert r_detail.status_code == 200
            body = r_detail.json()

            assert body["grade"] == "Extreme"
            assert body["score"] >= 75.0
            assert body["min_elev_m"] is not None and body["max_elev_m"] is not None
            assert body["elev_loss_m"] >= 0.0
            assert body["sustained_slope_deg"] is not None and body["sustained_slope_deg"] > 5.0
            assert body["is_4wd_only"] is True
            assert body["access_status"] == "public"
            assert body["bbox"] is not None and len(body["bbox"]) == 4
            assert len(body["source_refs"]) >= 2

            # Explainable baseline_breakdown must stay consistent with track header stats!
            bd = body["baseline_breakdown"]
            assert bd["formula_version"] == "baseline_v1"
            assert bd["final_score"] == body["score"]
            assert bd["grade"] == body["grade"] == "Extreme"
            assert bd["factors"]["max_slope"]["max_slope_deg"] >= 21.0
            assert bd["factors"]["max_slope"]["max_slope_deg"] == body["max_slope_deg"]
            assert bd["factors"]["sustained_relief"]["sustained_slope_deg"] == (
                body["sustained_slope_deg"]
            )
            assert bd["factors"]["waterway_crossings"]["unbridged_count"] >= 1
            assert bd["factors"]["waterway_crossings"]["points"] == 12
            assert "Murray River" in bd["factors"]["waterway_crossings"]["waterways"]
            assert bd["track_summary"]["segment_count"] == body["segment_count"]
            assert bd["track_summary"]["hardest_segment_id"] is not None
            assert bd["track_summary"]["steep_segment_count"] >= 1

            # Continuous strictly increasing elevation_profile
            profile = body["elevation_profile"]
            assert len(profile) > 100
            dists = [p["dist_m"] for p in profile]
            assert all(dists[i] < dists[i + 1] for i in range(len(dists) - 1))
            assert profile[0]["dist_m"] == 0.0
            assert abs(profile[-1]["dist_m"] - body["total_length_m"]) < 15.0

            # Waterway crossings (unbridged perennial Murray River)
            crossings = body["waterway_crossings"]
            assert any(
                c["name"] == "Murray River" and c["perennial"] is True and c["bridged"] is False
                for c in crossings
            )

            # Bridged Limestone Creek Link marks bridged=True
            r_bridged = client.get(f"/api/tracks/{bridged['track_id']}")
            assert r_bridged.status_code == 200
            bridged_ww = r_bridged.json()["waterway_crossings"]
            assert any(
                c["name"] == "Limestone Creek" and c["bridged"] is True for c in bridged_ww
            )

            # Nearby POIs within 1,500 m sorted ascending by distance_m
            pois = body["nearby_pois"]
            assert len(pois) >= 2
            poi_dists = [p["distance_m"] for p in pois]
            assert all(d is not None and d <= 1500.0 for d in poi_dists)
            assert poi_dists == sorted(poi_dists)
            assert any(p["name"] == "Tom Groggin Campground" for p in pois)

            # CAPAD tenure polygons (crosses Murray River border between Alpine NP & Kosciuszko NP)
            park_names = {pk["name"] for pk in body["capad_parks"]}
            assert {"Alpine National Park", "Kosciuszko National Park"}.issubset(park_names)

            # Unified search includes bbox, lon, lat, grade
            r_search = client.get("/api/search", params={"q": "Davies"})
            assert r_search.status_code == 200
            results = r_search.json()["results"]
            trk_hit = next(r for r in results if r["name"] == "Davies Plain Track Lower")
            assert trk_hit["kind"] == "track"
            assert trk_hit["grade"] == "Extreme"
            assert trk_hit["bbox"] is not None and len(trk_hit["bbox"]) == 4
            assert trk_hit["lon"] is not None and trk_hit["lat"] is not None
            poi_hit = next(r for r in results if r["name"] == "Davies Plain Hut")
            assert poi_hit["kind"] == "poi"
            assert poi_hit["category"] == "hut"
        app.dependency_overrides.clear()

    with postgis_engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    condition_report_votes, condition_reports,
                    telemetry_observations, unmapped_track_candidates,
                    segment_closure_links, closures_alerts,
                    segment_reconciliation_log, track_segment_grades,
                    pois, track_segments, tracks, capad_parks, users,
                    source_tracks_osm, source_tracks_vic, source_tracks_nsw,
                    source_waterways, source_pois, source_capad_parks
                RESTART IDENTITY CASCADE
                """
            )
        )
