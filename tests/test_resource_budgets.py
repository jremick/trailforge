"""Resource limits exercised offline with deliberately small budgets and ordinary data."""

from __future__ import annotations

import gzip
import json
import struct
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.test_matching_hmm import _build_offline_corridor_segments, _InMemoryCorridorHMMMatcher
from tests.test_route_planner import _truncate_route_test_tables
from trailforge.api.main import create_app
from trailforge.db.models import TrackSegment
from trailforge.db.session import get_db
from trailforge.grading import rainfall
from trailforge.ingestion.connectors import au_states, base, dem
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.pbf import StreamingPBFExtractor
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.ingestion.terrain import _decode_pure_python_geotiff
from trailforge.matching import hmm, imu, parse_gpx, trim_privacy_buffer
from trailforge.matching.protocol import MatchedSegmentTraversal, MatchResult
from trailforge.routes import planner


class _SmallStream(httpx.SyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


def test_pbf_download_reopens_only_complete_cached_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAILFORGE_PBF_MAX_DOWNLOAD_BYTES", "8")
    bodies = iter(
        [
            httpx.Response(200, stream=_SmallStream(b"1234", b"56789")),
            httpx.Response(200, headers={"Content-Length": "8"}, stream=_SmallStream(b"1234")),
            httpx.Response(200, stream=_SmallStream(b"1234", b"5678")),
            httpx.Response(200, stream=_SmallStream(b"abcd", b"efgh")),
        ]
    )
    with httpx.Client(transport=httpx.MockTransport(lambda _request: next(bodies))) as client:

        def ensure_pbf() -> Path:
            return StreamingPBFExtractor(
                cache_dir=tmp_path, pbf_url="https://fixtures.invalid/region.osm.pbf", client=client
            ).ensure_source_pbf()

        with pytest.raises(ValueError):
            ensure_pbf()
        with pytest.raises(ValueError):
            ensure_pbf()
        assert not list(tmp_path.rglob("*.pbf"))
        assert not list(tmp_path.rglob("*.part"))
        pbf = ensure_pbf()
        assert pbf.read_bytes() == b"12345678"
        assert ensure_pbf() == pbf
        pbf.write_bytes(b"1234")
        assert ensure_pbf().read_bytes() == b"abcdefgh"


def test_vector_queries_reject_over_budget_geometry_before_caching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://fixtures.invalid/query"
    cache = tmp_path / "features.json"
    feature = {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [
                [148.1, -36.5],
                [148.2, -36.6],
                [148.3, -36.7],
            ],
        },
        "properties": {},
    }
    body = json.dumps({"features": [feature]}).encode()

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, stream=_SmallStream(body[:64], body[64:]))
        )
    ) as client:
        monkeypatch.setattr(base, "_MAX_VECTOR_GEOMETRY_NODES", 2)
        assert (
            base.query_wfs_geojson(
                url, "trails", base.CORRIDOR_ALPS_BBOX, cache_path=cache, client=client
            )
            == []
        )
        assert not cache.exists()
        monkeypatch.setattr(base, "_MAX_VECTOR_GEOMETRY_NODES", 20)
        monkeypatch.setattr(base, "_MAX_VECTOR_PAGE_BYTES", 100)
        assert (
            base.query_arcgis_geojson(
                [url], base.CORRIDOR_ALPS_BBOX, cache_path=cache, client=client
            )
            == []
        )
        assert not cache.exists()
        monkeypatch.setattr(base, "_MAX_VECTOR_PAGE_BYTES", 1024)
        assert base.query_arcgis_geojson(
            [url], base.CORRIDOR_ALPS_BBOX, cache_path=cache, client=client
        ) == [feature]
        assert cache.exists()


def test_provider_follows_redirect_without_buffering_intermediate_body() -> None:
    class UnreadRedirectBody(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("Redirect body should stay unread")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/query":
            return httpx.Response(
                302, headers={"Location": "/features"}, stream=UnreadRedirectBody()
            )
        assert request.url.path == "/features"
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, json={"features": [{"geometry": {"type": "Point",
                               "coordinates": [148.1, -36.6]}}]})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        result = base.query_wfs_geojson(
            "https://fixtures.invalid/query", "trails", base.CORRIDOR_ALPS_BBOX, client=client
        )
    assert len(result) == 1

    def redirect_loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": str(request.url)})

    with (
        httpx.Client(transport=httpx.MockTransport(redirect_loop)) as client,
        pytest.raises(base.UpstreamLimitError, match="redirect limit"),
        base.stream_upstream(client, "https://fixtures.invalid/loop"),
    ):
        pass


def test_sa_trails_zip_checks_expanded_member_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    archive = tmp_path / "trails.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(au_states.DEFAULT_SA_TRAILS_ZIP_INNER, '{"features": []}')
    monkeypatch.setattr(au_states, "_MAX_SA_TRAILS_MEMBER_BYTES", 8)
    assert au_states.read_sa_trails_from_zip(archive, base.CORRIDOR_ALPS_BBOX) == []
    assert "expanded member size limit" in caplog.text


def test_rainfall_rejects_over_budget_stream_before_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rainfall, "_MAX_OPEN_METEO_BYTES", 32)
    data = b'{"hourly":{"time":["2026-11-26T12:00"],"precipitation":[1]}}'
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, stream=_SmallStream(data[:16], data[16:]))
            )
        ) as client,
        pytest.raises(ValueError, match="size limit"),
    ):
        rainfall.fetch_open_meteo_rainfall(
            148.1, -36.6, client=client, now=datetime(2026, 11, 26, 12, tzinfo=UTC)
        )


def test_dem_raster_budget_is_checked_before_loading_pixels(
    fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dem, "_MAX_DEM_PIXELS", 16)
    with pytest.raises(ValueError, match="pixel limit"):
        dem.inspect_raster_coverage(fixtures_dir / "dem_alps_corridor.tif")


def test_hmm_declines_dense_work_without_discarding_raw_point_capacity(
    fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    segments, _names = _build_offline_corridor_segments(fixtures_dir)
    matcher = _InMemoryCorridorHMMMatcher(segments)
    points = trim_privacy_buffer(
        parse_gpx((fixtures_dir / "sample_trace_davies_plain.gpx").read_bytes()).points
    ).trimmed_points
    assert matcher.match_trace_sync(points).matched_segments
    monkeypatch.setattr(hmm, "_MAX_HMM_CANDIDATE_EVALUATIONS", 1)
    with pytest.raises(hmm.MatchBudgetExceeded, match="Split the trip"):
        matcher.match_trace_sync(points)


def test_imu_refinement_keeps_aligned_measurements_when_windows_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 11, 26, 12, tzinfo=UTC)
    traversal = MatchedSegmentTraversal(
        segment_id="seg-a",
        direction="forward",
        entry_at=start,
        exit_at=start + timedelta(seconds=1),
        duration_s=1,
        avg_speed_kmh=10,
        roughness_rms=0.3,
        match_confidence=0.9,
        matched_point_count=1,
    )
    result = MatchResult(
        matcher_name="fixture",
        matched_segments=[traversal, traversal],
        unmatched_subtraces=[],
        overall_confidence=0.9,
        matched_fraction=1,
    )
    samples = [imu.ImuSample(t_ms=start.timestamp() * 1000, ax=0, ay=0, az=1)]
    monkeypatch.setattr(imu, "_MAX_SEGMENT_IMU_SAMPLE_VISITS", 2)
    assert imu.enrich_match_result_with_imu(result, samples) != result
    monkeypatch.setattr(imu, "_MAX_SEGMENT_IMU_SAMPLE_VISITS", 1)
    assert imu.enrich_match_result_with_imu(result, samples) == result


def test_route_limits_count_repeat_legs_before_building_their_geometry(
    postgis_engine: Engine, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _truncate_route_test_tables(postgis_engine)
    try:
        with Session(postgis_engine) as session:
            data = load_corridor_fixtures(fixtures_dir)
            ingest_corridor_dataset(session, data, commit=True)
            track_id, segment_count = session.execute(
                select(TrackSegment.track_id, func.count())
                .where(TrackSegment.track_id.is_not(None), TrackSegment.is_active.is_(True))
                .group_by(TrackSegment.track_id)
                .order_by(func.count().desc())
                .limit(1)
            ).one()
            app = create_app()
            app.dependency_overrides[get_db] = lambda: session
            with TestClient(app) as client:
                monkeypatch.setattr(planner, "_MAX_ROUTE_SEGMENTS", segment_count)
                one_leg = client.post("/api/routes/plan", json={"track_ids": [track_id]})
                assert one_leg.status_code == 200, one_leg.text
                two_legs = client.post("/api/routes/plan", json={"track_ids": [track_id, track_id]})
                assert two_legs.status_code == 422, two_legs.text
                assert "Split the route" in two_legs.json()["detail"]

                monkeypatch.setattr(planner, "_MAX_ROUTE_ANALYSIS_BYTES", 64)
                large_result = client.post("/api/routes/plan", json={"track_ids": [track_id]})
                assert large_result.status_code == 422, large_result.text
    finally:
        _truncate_route_test_tables(postgis_engine)


def test_iter_bounded_response_supports_empty_and_bounded_gzip_encoding() -> None:
    payload = b'{"features":[]}'
    gz_multi = gzip.compress(b'{"features":') + gzip.compress(b"[]}")
    gz_bomb = gzip.compress(b"A" * 4096)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/empty-enc":
            return httpx.Response(200, headers={"Content-Encoding": ""}, content=payload)
        if request.url.path == "/gzip-ok":
            return httpx.Response(
                200,
                headers={"Content-Encoding": "gzip", "Content-Length": str(len(gz_multi))},
                stream=_SmallStream(gz_multi[:15], gz_multi[15:]),
            )
        if request.url.path == "/gzip-bomb":
            return httpx.Response(
                200,
                headers={"Content-Encoding": "x-gzip"},
                stream=_SmallStream(gz_bomb),
            )
        return httpx.Response(200, headers={"Content-Encoding": "br"}, content=b"compressed")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with client.stream("GET", "https://fixtures.invalid/empty-enc") as resp:
            assert b"".join(
                base.iter_bounded_response(resp, max_bytes=128, max_duration_s=5.0)
            ) == payload

        with client.stream("GET", "https://fixtures.invalid/gzip-ok") as resp:
            assert b"".join(
                base.iter_bounded_response(resp, max_bytes=128, max_duration_s=5.0, chunk_bytes=8)
            ) == payload

        with (
            client.stream("GET", "https://fixtures.invalid/gzip-bomb") as resp,
            pytest.raises(base.UpstreamLimitError, match="size limit"),
        ):
            list(
                base.iter_bounded_response(
                    resp, max_bytes=256, max_duration_s=5.0, chunk_bytes=64
                )
            )

        with (
            client.stream("GET", "https://fixtures.invalid/br") as resp,
            pytest.raises(base.UpstreamLimitError, match="Unsupported upstream content encoding"),
        ):
            list(base.iter_bounded_response(resp, max_bytes=128, max_duration_s=5.0))


def test_plan_route_translates_statement_timeout_to_work_budget_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TimeoutOrig(Exception):
        sqlstate = "57014"

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise DBAPIError("SELECT 1", {}, _TimeoutOrig("statement timeout"))

    monkeypatch.setattr(planner, "_plan_route_with_geometry_impl", _boom)
    with pytest.raises(planner.RouteWorkBudgetExceeded, match="Split the route"):
        planner.plan_route_with_geometry(cast(Session, None), ["trk-1"])


def test_pure_python_geotiff_rejects_truncated_ifd_and_missing_tags(tmp_path: Path) -> None:
    # Header points IFD offset past end of file
    truncated_ifd = tmp_path / "truncated_ifd.tif"
    truncated_ifd.write_bytes(b"II" + struct.pack("<HI", 42, 999))
    with pytest.raises(ValueError, match="Truncated TIFF IFD header"):
        _decode_pure_python_geotiff(truncated_ifd)

    # Header claims 10 IFD entries without entry payload bytes
    truncated_table = tmp_path / "truncated_table.tif"
    truncated_table.write_bytes(b"II" + struct.pack("<HIH", 42, 8, 10))
    with pytest.raises(ValueError, match="Truncated TIFF IFD table"):
        _decode_pure_python_geotiff(truncated_table)

    # Valid empty IFD (0 entries) missing ImageWidth/ImageLength/StripOffsets/StripByteCounts
    empty_ifd = tmp_path / "empty_ifd.tif"
    empty_ifd.write_bytes(b"II" + struct.pack("<HIH", 42, 8, 0))
    with pytest.raises(ValueError, match="Missing required TIFF tag"):
        _decode_pure_python_geotiff(empty_ifd)
