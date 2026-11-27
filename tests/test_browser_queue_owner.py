"""Account binding for the browser's location-bearing write entry points."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from trailforge.api.main import create_app

_POINTS = [
    {"lon": 148.10, "lat": -36.50, "timestamp": "2026-11-25T01:00:00Z"},
    {"lon": 148.12, "lat": -36.51, "timestamp": "2026-11-25T01:10:00Z"},
    {"lon": 148.14, "lat": -36.52, "timestamp": "2026-11-25T01:20:00Z"},
]
_TRIP = {"name": "Practice route", "visibility": "private", "points": _POINTS}


@pytest.mark.integration
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/trips/mobile-sync", _TRIP),
        ("/api/trips/upload", _TRIP),
        ("/api/reports", {
            "report_type": "fallen_tree", "notes": "Synthetic branch",
            "lon": 148.12, "lat": -36.51,
        }),
        ("/api/pins", {
            "name": "Practice camp", "category": "camp",
            "lon": 148.12, "lat": -36.51,
        }),
    ],
)
def test_browser_location_write_requires_its_account(
    postgis_engine: Engine, path: str, payload: dict[str, object]
) -> None:
    with TestClient(create_app()) as browser:
        origin = {"Origin": str(browser.base_url)}
        registration = browser.post("/api/auth/register", json={
            "email": f"queue-{uuid.uuid4().hex}@example.test",
            "display_name": "Synthetic recorder",
            "password": "Fixture passphrase 47!",
        }, headers=origin)
        assert registration.status_code == 201, registration.text
        account = registration.json()
        owner = account["user"]["id"]

        blocked = browser.post(path, json=payload, headers=origin)
        assert blocked.status_code == 428, blocked.text
        assert blocked.json()["detail"] == "Refresh TrailForge before saving to your account"

        saved = browser.post(path, json=payload, headers={**origin, "X-TrailForge-Account": owner})
        assert saved.status_code == 201, saved.text

        bearer = {"Authorization": f"Bearer {account['access_token']}"}
        with TestClient(create_app()) as mobile:
            saved_without_cookie = mobile.post(path, json=payload, headers=bearer)
            assert saved_without_cookie.status_code == 201, saved_without_cookie.text

            fetch_metadata = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"}
            web = {**bearer, **fetch_metadata, "Origin": "https://dashboard.example.test"}
            old_web_bundle = mobile.post(path, json=payload, headers=web)
            assert old_web_bundle.status_code == 428, old_web_bundle.text
            new_web_bundle = mobile.post(
                path, json=payload, headers={**web, "X-TrailForge-Account": owner}
            )
            assert new_web_bundle.status_code == 201, new_web_bundle.text

            for native_origin in ("capacitor://localhost", "https://localhost"):
                native = mobile.post(
                    path, json=payload,
                    headers={**bearer, **fetch_metadata, "Origin": native_origin},
                )
                assert native.status_code == 201, native.text
