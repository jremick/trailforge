"""Offline unit & PostGIS tests for UI preferences, admin defaults, and closure details."""

from __future__ import annotations

import importlib.util
import re
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from trailforge.api.main import create_app
from trailforge.auth.security import create_access_token, hash_password
from trailforge.config import get_settings
from trailforge.db.models import ClosureAlert, User
from trailforge.db.session import get_db
from trailforge.feeds.queries import (
    clean_closure_text,
    extract_closure_subtitle,
    extract_incident_details,
)
from trailforge.feeds.sync import sync_from_fixtures
from trailforge.ingestion.connectors import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.ui_preferences import (
    CANONICAL_LAYER_KEYS,
    DEFAULT_LAYER_VISIBILITY,
    get_public_ui_config,
    normalize_layer_order,
    normalize_layer_visibility,
    validate_layer_order,
    validate_layer_visibility,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_0006_PATH = (
    REPO_ROOT / "migrations" / "versions" / "0006_ui_preferences_and_settings.py"
)


# -----------------------------------------------------------------------------
# Pure Offline Unit Tests (Zero DB, Zero Network)
# -----------------------------------------------------------------------------


def test_canonical_layer_order_matches_requested_defaults() -> None:
    assert CANONICAL_LAYER_KEYS == (
        "placesRegions",
        "highways",
        "contoursHillshade",
        "parksWaterways",
        "pois",
        "tracks",
        "steepHighlights",
        "hotspots",
        "closures",
        "reports",
        "unmapped",
        "snowLine",
    )
    assert set(DEFAULT_LAYER_VISIBILITY.keys()) == set(CANONICAL_LAYER_KEYS)
    assert DEFAULT_LAYER_VISIBILITY["placesRegions"] is True
    assert DEFAULT_LAYER_VISIBILITY["highways"] is True
    assert DEFAULT_LAYER_VISIBILITY["unmapped"] is False
    assert DEFAULT_LAYER_VISIBILITY["snowLine"] is False
    assert DEFAULT_LAYER_VISIBILITY["tracks"] is True


def test_canonical_layer_parity_between_backend_and_frontend() -> None:
    """Guarantee 1:1 canonical layer key order and default visibility parity with web UI."""
    ts_path = REPO_ROOT / "web" / "src" / "layerPrefsHelpers.ts"
    ts_src = ts_path.read_text(encoding="utf-8")

    order_match = re.search(
        r"export const CANONICAL_LAYER_ORDER:\s*readonly LayerKey\[\]\s*=\s*\[(.*?)\]\s*as const;",
        ts_src,
        re.DOTALL,
    )
    assert order_match is not None, "Could not locate CANONICAL_LAYER_ORDER in layerPrefsHelpers.ts"
    frontend_order = tuple(re.findall(r'"([A-Za-z0-9_]+)"', order_match.group(1)))
    assert frontend_order == CANONICAL_LAYER_KEYS

    vis_match = re.search(
        r"export const DEFAULT_LAYER_VISIBILITY:\s*Readonly<LayerVisibilityMap>\s*=\s*\{(.*?)\};",
        ts_src,
        re.DOTALL,
    )
    assert vis_match is not None, "Could not locate DEFAULT_LAYER_VISIBILITY in layerPrefsHelpers.ts"
    frontend_vis = {
        k: (v == "true")
        for k, v in re.findall(r"([A-Za-z0-9_]+)\s*:\s*(true|false)", vis_match.group(1))
    }
    assert frontend_vis == DEFAULT_LAYER_VISIBILITY


def test_validate_and_normalize_layer_preferences() -> None:
    # Strict validation appends omitted canonical keys in canonical order
    ordered = validate_layer_order(["snowLine", "tracks"])
    assert ordered[:2] == ["snowLine", "tracks"]
    assert len(ordered) == len(CANONICAL_LAYER_KEYS)
    assert set(ordered) == set(CANONICAL_LAYER_KEYS)

    with pytest.raises(ValueError, match="Unknown map layer key"):
        validate_layer_order(["tracks", "not_a_layer"])

    with pytest.raises(ValueError, match="Duplicate map layer key"):
        validate_layer_order(["tracks", "tracks"])

    with pytest.raises(ValueError, match="at least one"):
        validate_layer_order([])

    vis = validate_layer_visibility(
        {"snowLine": True, "tracks": False, "placesRegions": False, "highways": True}
    )
    assert vis == {
        "snowLine": True,
        "tracks": False,
        "placesRegions": False,
        "highways": True,
    }

    with pytest.raises(ValueError, match="Unknown map layer key"):
        validate_layer_visibility({"bogus": True})

    with pytest.raises(ValueError, match="must be a boolean"):
        validate_layer_visibility({"tracks": "yes"})

    # Lenient normalization recovers safely from corrupted/partial values
    assert normalize_layer_order(["pois", "invalid", "pois"])[:1] == ["pois"]
    assert len(normalize_layer_order(["pois", "invalid", "pois"])) == len(CANONICAL_LAYER_KEYS)

    norm_vis = normalize_layer_visibility({"snowLine": True, "tracks": "bad", "extra": True})
    assert norm_vis["snowLine"] is True
    assert norm_vis["tracks"] is True  # fell back to default


def test_ui_config_env_default_for_stack_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRAILFORGE_SHOW_STACK_TELEMETRY_PUBLIC", raising=False)
    monkeypatch.delenv("SHOW_STACK_TELEMETRY_PUBLIC", raising=False)
    get_settings.cache_clear()
    default_cfg = get_public_ui_config(None)
    assert default_cfg["show_stack_telemetry_public"] is False
    assert default_cfg["default_layer_order"] == list(CANONICAL_LAYER_KEYS)

    monkeypatch.setenv("TRAILFORGE_SHOW_STACK_TELEMETRY_PUBLIC", "true")
    get_settings.cache_clear()
    enabled_cfg = get_public_ui_config(None)
    assert enabled_cfg["show_stack_telemetry_public"] is True
    get_settings.cache_clear()


def test_closure_text_cleaning_and_incident_metadata_extraction() -> None:
    raw_html = (
        "<p><strong>Watch &amp; Act</strong> — Bushfire burning near Bucky Ridge.</p>"
        "<p>Avoid Alpine roads.<br/>Monitor VicEmergency for updates.</p>"
    )
    cleaned = clean_closure_text(raw_html)
    assert cleaned is not None
    assert "<p>" not in cleaned
    assert "<strong>" not in cleaned
    assert "Watch & Act — Bushfire burning near Bucky Ridge." in cleaned
    assert "\n" in cleaned

    payload = {
        "sourceTitle": "Advice",
        "webHeadline": "Bushfire near Mount Hotham",
        "location": "Dargo High Plains Rd, Mount Hotham",
        "category1": "Bushfire",
        "category2": "Grass Fire",
        "sizeFmt": "42 ha",
        "resources": 8,
        "status": "Going",
        "council": "Alpine Shire",
    }
    details = extract_incident_details(payload, title="Advice")
    assert details["headline"] == "Bushfire near Mount Hotham"
    assert details["location"] == "Dargo High Plains Rd, Mount Hotham"
    assert details["category"] == "Bushfire · Grass Fire"
    assert details["size"] == "42 ha"
    assert details["resources"] == "8"
    assert details["council_area"] == "Alpine Shire"
    # Subtitle prioritizes headline when title is generic ("Advice")
    assert extract_closure_subtitle("Advice", payload) == "Bushfire near Mount Hotham"


def test_migration_0006_revises_0005() -> None:
    assert MIGRATION_0006_PATH.is_file()
    spec = importlib.util.spec_from_file_location("migration_0006", MIGRATION_0006_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0006_ui_preferences_and_settings"
    assert mod.down_revision == "0005_user_token_version"


def test_ui_config_admin_settings_and_user_preferences_api_offline_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise /api/ui-config, /api/admin/ui-settings, and /api/user/preferences end-to-end."""
    monkeypatch.delenv("TRAILFORGE_SHOW_STACK_TELEMETRY_PUBLIC", raising=False)
    get_settings.cache_clear()

    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE users (
                    id CHAR(32) PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role VARCHAR(16) NOT NULL DEFAULT 'member',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    token_version INTEGER NOT NULL DEFAULT 1,
                    disabled_at DATETIME,
                    invited_by_code VARCHAR(64),
                    ui_preferences JSON NOT NULL DEFAULT '{}',
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE instance_ui_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    default_layer_order JSON,
                    default_layer_visibility JSON,
                    show_stack_telemetry_public BOOLEAN,
                    updated_by_user_id CHAR(32) REFERENCES users(id) ON DELETE SET NULL,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )

    admin_headers = _create_user_and_headers(
        engine, email="admin-sqlite@example.test", role="admin", display_name="SQLite Admin"
    )
    member_headers = _create_user_and_headers(
        engine, email="member-sqlite@example.test", role="member", display_name="SQLite Member"
    )

    app = create_app()

    def _override_db() -> Generator[Session, None, None]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    try:
        with TestClient(app) as client:
            pub = client.get("/api/ui-config")
            assert pub.status_code == 200
            assert pub.json()["default_layer_order"] == list(CANONICAL_LAYER_KEYS)
            assert pub.json()["show_stack_telemetry_public"] is False

            # Authorization checks on admin endpoint
            assert client.get("/api/admin/ui-settings").status_code == 401
            assert client.get("/api/admin/ui-settings", headers=member_headers).status_code == 403

            # Admin updates defaults
            new_order = [
                "tracks",
                "placesRegions",
                "highways",
                "contoursHillshade",
                "parksWaterways",
                "pois",
                "steepHighlights",
                "hotspots",
                "closures",
                "reports",
                "unmapped",
                "snowLine",
            ]
            put_admin = client.put(
                "/api/admin/ui-settings",
                headers=admin_headers,
                json={
                    "default_layer_order": new_order,
                    "default_layer_visibility": {
                        "snowLine": True,
                        "tracks": False,
                        "highways": False,
                    },
                    "show_stack_telemetry_public": True,
                },
            )
            assert put_admin.status_code == 200
            assert put_admin.json()["default_layer_order"] == new_order
            assert put_admin.json()["default_layer_visibility"]["snowLine"] is True
            assert put_admin.json()["default_layer_visibility"]["tracks"] is False
            assert put_admin.json()["default_layer_visibility"]["highways"] is False
            assert put_admin.json()["default_layer_visibility"]["placesRegions"] is True
            assert put_admin.json()["show_stack_telemetry_public"] is True

            # Public config reflects admin update
            pub2 = client.get("/api/ui-config")
            assert pub2.status_code == 200
            assert pub2.json()["default_layer_order"] == new_order
            assert pub2.json()["show_stack_telemetry_public"] is True

            # Member preferences CRUD & validation
            assert client.get("/api/user/preferences").status_code == 401
            u0 = client.get("/api/user/preferences", headers=member_headers)
            assert u0.status_code == 200
            assert u0.json()["has_custom_preferences"] is False

            u1 = client.put(
                "/api/user/preferences",
                headers=member_headers,
                json={
                    "layer_order": ["pois", "tracks"],
                    "layer_visibility": {"unmapped": True},
                },
            )
            assert u1.status_code == 200
            assert u1.json()["layer_order"][:2] == ["pois", "tracks"]
            assert u1.json()["layer_visibility"] == {"unmapped": True}
            assert u1.json()["has_custom_preferences"] is True

            bad = client.put(
                "/api/user/preferences",
                headers=member_headers,
                json={"layer_order": ["not_real"]},
            )
            assert bad.status_code == 422

            u2 = client.delete("/api/user/preferences", headers=member_headers)
            assert u2.status_code == 200
            assert u2.json()["has_custom_preferences"] is False

            # Admin reset reverts to factory & env defaults
            reset_admin = client.delete("/api/admin/ui-settings", headers=admin_headers)
            assert reset_admin.status_code == 200
            assert reset_admin.json()["default_layer_order"] == list(CANONICAL_LAYER_KEYS)
            assert reset_admin.json()["show_stack_telemetry_public"] is False
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


# -----------------------------------------------------------------------------
# PostGIS Integration Tests (100% Offline)
# -----------------------------------------------------------------------------


def _truncate_tables(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    instance_ui_settings,
                    segment_closure_links, closures_alerts, feed_sync_status,
                    track_segment_grades, segment_reconciliation_log,
                    planned_routes, user_favorites, user_pins, vehicle_profiles,
                    invite_codes, usage_metrics_hourly, users,
                    pois, track_segments, tracks, capad_parks,
                    source_tracks_osm, source_tracks_vic, source_tracks_nsw,
                    source_tracks_au_state,
                    source_waterways, source_pois, source_capad_parks
                RESTART IDENTITY CASCADE
                """
            )
        )


def _create_user_and_headers(
    engine: Engine, *, email: str, role: str, display_name: str
) -> dict[str, str]:
    with Session(engine) as session:
        user = User(
            id=uuid.uuid4(),
            email=email,
            display_name=display_name,
            password_hash=hash_password("TestPass123!"),
            role=role,
            is_active=True,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        token = create_access_token(
            str(user.id),
            user.email,
            token_version=int(user.token_version or 1),
        )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.integration
def test_admin_ui_settings_and_user_preferences_lifecycle(
    postgis_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _truncate_tables(postgis_engine)
    monkeypatch.delenv("TRAILFORGE_SHOW_STACK_TELEMETRY_PUBLIC", raising=False)
    get_settings.cache_clear()

    admin_headers = _create_user_and_headers(
        postgis_engine, email="admin-ui@example.test", role="admin", display_name="UI Admin"
    )
    member_headers = _create_user_and_headers(
        postgis_engine, email="member-ui@example.test", role="member", display_name="UI Member"
    )

    app = create_app()

    def _override_db() -> Generator[Session, None, None]:
        with Session(postgis_engine) as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    try:
        with TestClient(app) as client:
            # 1. Public UI config starts at factory defaults
            pub = client.get("/api/ui-config")
            assert pub.status_code == 200
            assert pub.json() == {
                "default_layer_order": list(CANONICAL_LAYER_KEYS),
                "default_layer_visibility": dict(DEFAULT_LAYER_VISIBILITY),
                "show_stack_telemetry_public": False,
            }

            # 2. Non-admin cannot touch admin UI settings
            assert client.get("/api/admin/ui-settings").status_code == 401
            assert client.get("/api/admin/ui-settings", headers=member_headers).status_code == 403
            assert (
                client.put(
                    "/api/admin/ui-settings",
                    headers=member_headers,
                    json={"show_stack_telemetry_public": True},
                ).status_code
                == 403
            )

            # 3. Admin configures custom default order, visibility, and public stack telemetry
            custom_order = [
                "tracks",
                "closures",
                "placesRegions",
                "highways",
                "contoursHillshade",
                "parksWaterways",
                "pois",
                "steepHighlights",
                "hotspots",
                "reports",
                "unmapped",
                "snowLine",
            ]
            saved = client.put(
                "/api/admin/ui-settings",
                headers=admin_headers,
                json={
                    "default_layer_order": custom_order,
                    "default_layer_visibility": {"snowLine": True, "hotspots": False},
                    "show_stack_telemetry_public": True,
                },
            )
            assert saved.status_code == 200
            body = saved.json()
            assert body["default_layer_order"] == custom_order
            assert body["default_layer_visibility"]["snowLine"] is True
            assert body["default_layer_visibility"]["hotspots"] is False
            assert body["show_stack_telemetry_public"] is True
            assert body["has_custom_layer_order"] is True
            assert body["has_custom_layer_visibility"] is True

            # Public endpoint immediately reflects admin defaults
            pub2 = client.get("/api/ui-config")
            assert pub2.status_code == 200
            assert pub2.json()["default_layer_order"] == custom_order
            assert pub2.json()["default_layer_visibility"]["snowLine"] is True
            assert pub2.json()["show_stack_telemetry_public"] is True

            # 4. Member personal preferences CRUD
            assert client.get("/api/user/preferences").status_code == 401
            pref0 = client.get("/api/user/preferences", headers=member_headers)
            assert pref0.status_code == 200
            assert pref0.json() == {
                "layer_order": None,
                "layer_visibility": None,
                "has_custom_preferences": False,
            }

            user_order = ["pois", "contoursHillshade", "tracks"]
            pref1 = client.put(
                "/api/user/preferences",
                headers=member_headers,
                json={
                    "layer_order": user_order,
                    "layer_visibility": {"unmapped": True, "tracks": False},
                },
            )
            assert pref1.status_code == 200
            p1 = pref1.json()
            assert p1["layer_order"][:3] == user_order
            assert p1["layer_visibility"] == {"unmapped": True, "tracks": False}
            assert p1["has_custom_preferences"] is True

            # Invalid layer key rejected cleanly with 422
            bad = client.put(
                "/api/user/preferences",
                headers=member_headers,
                json={"layer_visibility": {"unknown_layer": True}},
            )
            assert bad.status_code == 422

            # Delete resets user preferences cleanly
            cleared = client.delete("/api/user/preferences", headers=member_headers)
            assert cleared.status_code == 200
            assert cleared.json()["has_custom_preferences"] is False

            # 5. Admin reset reverts public config back to factory + env defaults
            reset_resp = client.delete("/api/admin/ui-settings", headers=admin_headers)
            assert reset_resp.status_code == 200
            assert reset_resp.json()["default_layer_order"] == list(CANONICAL_LAYER_KEYS)
            assert reset_resp.json()["show_stack_telemetry_public"] is False
    finally:
        app.dependency_overrides.clear()


@pytest.mark.integration
def test_closure_detail_endpoint_returns_incident_metadata_and_tracks(
    postgis_engine: Engine,
    fixtures_dir: Path,
) -> None:
    _truncate_tables(postgis_engine)

    with Session(postgis_engine) as session:
        dataset = load_corridor_fixtures(fixtures_dir)
        ingest_corridor_dataset(session, dataset)
        sync_from_fixtures(session, fixtures_dir / "closures_sample.json")
        vicem = session.execute(
            select(ClosureAlert).where(ClosureAlert.source_feed == "vic_emergency").limit(1)
        ).scalar_one()
        vicem_id = int(vicem.id)

    app = create_app()

    def _override_db() -> Generator[Session, None, None]:
        with Session(postgis_engine) as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    try:
        with TestClient(app) as client:
            resp = client.get(f"/api/closures/{vicem_id}")
            assert resp.status_code == 200
            detail = resp.json()
            assert detail["closure_id"] == vicem_id
            assert detail["feed_source"] == "vic_emergency"
            assert detail["agency"] == "VicEmergency"
            assert detail["title"] == "Bucky Ridge — Planned Fuel Reduction Burn Watch & Act"
            assert detail["closure_type"] == "incident"
            assert detail["severity"] == "warning"
            assert detail["state"] == "VIC"
            assert detail["bbox"] is not None
            assert len(detail["bbox"]) == 4
            assert isinstance(detail["affected_tracks"], list)
            assert isinstance(detail["affected_segment_count"], int)

            missing = client.get("/api/closures/99999999")
            assert missing.status_code == 404
    finally:
        app.dependency_overrides.clear()
