"""Account binding for the browser's location-bearing write entry points."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from starlette.requests import Request

from trailforge.api.main import create_app
from trailforge.auth.dependencies import (
    _is_web_session_bearer,
    _require_browser_queue_account,
    require_expected_account,
)
from trailforge.auth.tokens import OAUTH_ACCESS_PREFIX, PAT_PREFIX
from trailforge.db.models import User


def _fake_request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/pins",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
    }
    return Request(scope)


def test_browser_queue_account_guard_unit_cookie_bearer_native_and_pat() -> None:
    owner_id = uuid.uuid4()
    user = User(id=owner_id, email="driver@example.test", display_name="Driver", password_hash="x")

    # Stale account header -> 409; matching header -> OK
    with pytest.raises(HTTPException) as stale:
        require_expected_account(_fake_request({"X-TrailForge-Account": str(uuid.uuid4())}), user)
    assert stale.value.status_code == 409
    require_expected_account(_fake_request({"X-TrailForge-Account": str(owner_id)}), user)

    # Cookie-only browser write without X-TrailForge-Account -> 428
    with pytest.raises(HTTPException) as cookie_blocked:
        _require_browser_queue_account(_fake_request({"Cookie": "tf_session=abc"}))
    assert cookie_blocked.value.status_code == 428

    # Cookie write with X-TrailForge-Account -> allowed
    _require_browser_queue_account(
        _fake_request({"Cookie": "tf_session=abc", "X-TrailForge-Account": str(owner_id)})
    )

    # Cross-origin web session bearer with Fetch Metadata -> 428 without header
    cors_meta = {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "cross-site"}
    web_req = _fake_request(
        {"Authorization": "Bearer session.tok", **cors_meta, "Origin": "https://app.example.test"}
    )
    assert _is_web_session_bearer(web_req, "session.tok") is True
    with pytest.raises(HTTPException) as web_blocked:
        _require_browser_queue_account(web_req)
    assert web_blocked.value.status_code == 428

    # Native Capacitor origins, PAT/OAuth bearers, CLI without Fetch Metadata, and bad Origin -> OK
    for origin in ("capacitor://localhost", "https://localhost", "http://127.0.0.1:5173", "http://[bad"):
        req = _fake_request({"Authorization": "Bearer session.tok", **cors_meta, "Origin": origin})
        assert _is_web_session_bearer(req, "session.tok") is False
        _require_browser_queue_account(req)

    for delegated in (f"{PAT_PREFIX}abc", f"{OAUTH_ACCESS_PREFIX}abc"):
        req = _fake_request(
            {"Authorization": f"Bearer {delegated}", **cors_meta, "Origin": "https://app.example.test"}
        )
        assert _is_web_session_bearer(req, delegated) is False
        _require_browser_queue_account(req)

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
