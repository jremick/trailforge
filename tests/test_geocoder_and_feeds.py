"""Offline regression tests for gzip feed decompression and AU geocoder fallback."""

from __future__ import annotations

import gzip
import json
from typing import Any

import httpx
import pytest

from trailforge.api.geocoder import geocode_au_places, reset_geocoder_cache
from trailforge.feeds.adapters import NSWRFSAdapter, VicEmergencyAdapter


def test_http_feed_adapter_handles_gzip_content_encoding_without_double_decompress() -> None:
    """Streaming resp.iter_bytes() decodes gzip once; copied headers must drop Content-Encoding."""
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "id": "vic-evt-1",
                    "name": "Alpine Ridge Bushfire",
                    "status": "Going",
                    "category1": "Bushfire",
                    "category2": "Advice",
                    "feedType": "incident",
                },
                "geometry": {"type": "Point", "coordinates": [147.13, -37.39]},
            }
        ],
    }
    compressed = gzip.compress(json.dumps(payload).encode("utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
                "Content-Length": str(len(compressed)),
            },
            content=compressed,
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        adapter = VicEmergencyAdapter(client=client)
        parsed = adapter._fetch_json()
        assert parsed["type"] == "FeatureCollection"
        assert len(parsed["features"]) == 1
        assert parsed["features"][0]["properties"]["name"] == "Alpine Ridge Bushfire"

        rfs_adapter = NSWRFSAdapter(client=client)
        parsed_rfs = rfs_adapter._fetch_json()
        assert parsed_rfs["type"] == "FeatureCollection"
    finally:
        client.close()


def test_http_feed_adapter_still_enforces_max_bytes_on_gzip_stream() -> None:
    """Decompressed byte cap must still reject oversized gzipped responses (zip-bomb defense)."""
    oversized = gzip.compress(b"x" * 2048)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            headers={"Content-Encoding": "gzip"},
            content=oversized,
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        adapter = VicEmergencyAdapter(client=client)
        with pytest.raises(ValueError, match="byte limit"):
            adapter._http_get(max_bytes=512)
    finally:
        client.close()


def test_au_geocoder_parses_nominatim_bbox_state_and_caches() -> None:
    """Nominatim fallback converts [s,n,w,e] -> [w,s,e,n], normalizes VIC, and caches hits."""
    reset_geocoder_cache()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.params.get("countrycodes") == "au"
        assert request.url.params.get("bounded") == "1"
        body: list[dict[str, Any]] = [
            {
                "osm_type": "way",
                "osm_id": 55443322,
                "name": "Billy Goat Bluff Track",
                "display_name": "Billy Goat Bluff Track, Dargo, Victoria, Australia",
                "lat": "-37.3921",
                "lon": "147.1314",
                "boundingbox": ["-37.4200", "-37.3600", "147.1100", "147.1600"],
                "type": "track",
                "address": {"town": "Dargo", "state": "Victoria", "ISO3166-2-lvl4": "AU-VIC"},
            },
            {
                "osm_type": "node",
                "osm_id": 999,
                "name": "Outside Australia",
                "display_name": "London, UK",
                "lat": "51.5074",
                "lon": "-0.1278",
                "boundingbox": ["51.4", "51.6", "-0.2", "0.0"],
                "type": "city",
                "address": {"state": "England"},
            },
        ]
        return httpx.Response(status_code=200, json=body, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        first = geocode_au_places(
            "Billy Goat Bluff", limit=5, client=client, bypass_rate_limit=True
        )
        assert len(first) == 1
        place = first[0]
        assert place.id == "nominatim:way:55443322"
        assert place.name == "Billy Goat Bluff Track"
        assert place.state == "VIC"
        assert place.bbox == [147.11, -37.42, 147.16, -37.36]
        assert "Dargo" in place.subtitle
        assert calls == 1

        # Second identical lookup must hit in-memory LRU cache (0 additional HTTP calls)
        second = geocode_au_places(
            "  billy   goat bluff ", limit=5, client=client, bypass_rate_limit=True
        )
        assert len(second) == 1
        assert second[0].id == place.id
        assert calls == 1

        # Short query (< 3 chars) skips Nominatim
        assert geocode_au_places("bi", limit=5, client=client, bypass_rate_limit=True) == []
        assert calls == 1
    finally:
        client.close()
        reset_geocoder_cache()


def test_au_geocoder_fails_open_on_upstream_error() -> None:
    """Upstream timeouts or 503s must return [] cleanly instead of failing /api/search."""
    reset_geocoder_cache()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        assert (
            geocode_au_places("Blue Rag Range", limit=5, client=client, bypass_rate_limit=True)
            == []
        )
    finally:
        client.close()
        reset_geocoder_cache()
