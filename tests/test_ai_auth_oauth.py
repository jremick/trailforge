"""100% offline unit & FastAPI tests for SC-6 (PATs) and SC-7 (OAuth 2.1 + PKCE + DCR)."""

from __future__ import annotations

import importlib.util
import re
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from trailforge.api.auth import router as auth_router
from trailforge.api.oauth import oauth_router, tokens_router, well_known_router
from trailforge.auth.oauth import (
    OAuthError,
    build_authorization_server_metadata,
    build_protected_resource_metadata,
    build_www_authenticate_header,
    compute_pkce_s256,
    create_oauth_csrf_nonce,
    exchange_authorization_code,
    issue_authorization_code,
    prune_stale_oauth_clients,
    register_oauth_client,
    resolve_public_origin,
    validate_authorize_request,
    validate_code_challenge,
    validate_redirect_uri,
    verify_oauth_csrf_nonce,
    verify_pkce_s256,
)
from trailforge.auth.security import create_access_token, hash_password
from trailforge.auth.tokens import (
    MAX_ACTIVE_PATS_PER_USER,
    OAUTH_ACCESS_PREFIX,
    OAUTH_CODE_PREFIX,
    OAUTH_REFRESH_PREFIX,
    PAT_PREFIX,
    SCOPE_READ,
    SCOPE_WRITE,
    McpAuthContext,
    TokenLimitExceededError,
    create_personal_access_token,
    hash_token,
    normalize_scopes,
    require_scope,
    resolve_auth_context,
    resolve_bearer_token,
)
from trailforge.config import Settings, get_settings
from trailforge.db.models import User
from trailforge.db.models_ai_auth import (
    OAuthAuthorizationCode,
    OAuthClient,
    OAuthToken,
    PersonalAccessToken,
)
from trailforge.db.session import get_db

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_0006_PATH = REPO_ROOT / "migrations" / "versions" / "0006_ai_mcp_auth.py"

_CSRF_INPUT_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


# -----------------------------------------------------------------------------
# SQLite Offline Test Harness (Zero External DB or Network Required)
# -----------------------------------------------------------------------------


def _create_sqlite_ai_auth_schema(engine: Engine) -> None:
    """Create SQLite-compatible DDL matching users + 4 Tier C AI auth tables."""
    ddl_statements = (
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
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE vehicle_profiles (
            id CHAR(32) PRIMARY KEY,
            user_id CHAR(32) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            is_default BOOLEAN NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE personal_access_tokens (
            id CHAR(32) PRIMARY KEY,
            user_id CHAR(32) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            user_token_version INTEGER NOT NULL DEFAULT 1,
            name VARCHAR(80) NOT NULL,
            token_prefix VARCHAR(16) NOT NULL,
            token_hash VARCHAR(64) NOT NULL UNIQUE,
            scopes JSON NOT NULL,
            expires_at DATETIME,
            last_used_at DATETIME,
            revoked_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE oauth_clients (
            client_id VARCHAR(64) PRIMARY KEY,
            client_name VARCHAR(120) NOT NULL,
            client_secret_hash VARCHAR(64),
            redirect_uris JSON NOT NULL,
            grant_types JSON NOT NULL,
            token_endpoint_auth_method VARCHAR(32) NOT NULL
                CHECK (token_endpoint_auth_method IN ('none','client_secret_post')),
            registered_by_ip_hash VARCHAR(64),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at DATETIME
        )
        """,
        """
        CREATE TABLE oauth_authorization_codes (
            code_hash VARCHAR(64) PRIMARY KEY,
            client_id VARCHAR(64) NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
            user_id CHAR(32) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            redirect_uri TEXT NOT NULL,
            scope TEXT NOT NULL,
            code_challenge VARCHAR(128) NOT NULL,
            code_challenge_method VARCHAR(16) NOT NULL CHECK (code_challenge_method = 'S256'),
            resource TEXT,
            expires_at DATETIME NOT NULL,
            consumed_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE oauth_tokens (
            id CHAR(32) PRIMARY KEY,
            token_family_id CHAR(32) NOT NULL,
            client_id VARCHAR(64) NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
            user_id CHAR(32) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            user_token_version INTEGER NOT NULL,
            access_token_hash VARCHAR(64) NOT NULL UNIQUE,
            refresh_token_hash VARCHAR(64) UNIQUE,
            scope TEXT NOT NULL,
            resource TEXT,
            access_expires_at DATETIME NOT NULL,
            refresh_expires_at DATETIME,
            revoked_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
    )
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON"))
        for stmt in ddl_statements:
            conn.execute(text(stmt))


@pytest.fixture
def sqlite_db() -> Generator[Session, None, None]:
    engine = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _create_sqlite_ai_auth_schema(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _create_user(
    db: Session,
    *,
    email: str = "driver@example.com",
    display_name: str = "Alpine Driver",
    password: str = "HighCountry4WD!",
    is_active: bool = True,
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        display_name=display_name,
        password_hash=hash_password(password),
        role="member",
        is_active=is_active,
        token_version=1,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _build_test_app(db: Session, *, custom_settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="TrailForge AI Auth Test Harness")
    if custom_settings is not None:
        app.state.custom_settings = custom_settings

    app.include_router(auth_router)
    app.include_router(well_known_router)
    app.include_router(oauth_router)
    app.include_router(tokens_router)

    def _override_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = _override_db

    # Lightweight mock /mcp & /mcp/auth endpoints exercising unified Bearer resolver + scope gates
    @app.get("/mcp")
    def mock_mcp_read(request: Request, session: Session = Depends(get_db)) -> dict[str, object]:
        ctx = resolve_auth_context(request, session, allow_cookie=False)
        return {
            "authenticated": ctx.is_authenticated,
            "auth_method": ctx.auth_method,
            "user_id": str(ctx.user.id) if ctx.user else None,
            "scopes": sorted(ctx.scopes),
            "client_id": ctx.client_id,
        }

    @app.get("/mcp/auth")
    def mock_mcp_auth_read(
        request: Request, session: Session = Depends(get_db)
    ) -> dict[str, object]:
        ctx = resolve_auth_context(request, session, allow_cookie=False)
        if not ctx.is_authenticated:
            origin = resolve_public_origin(request, settings=custom_settings)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={
                    "WWW-Authenticate": build_www_authenticate_header(
                        origin, resource_path="/mcp/auth"
                    )
                },
            )
        require_scope(ctx, SCOPE_READ)
        return {
            "authenticated": True,
            "auth_method": ctx.auth_method,
            "user_id": str(ctx.user.id) if ctx.user else None,
            "scopes": sorted(ctx.scopes),
            "client_id": ctx.client_id,
        }

    @app.post("/mcp/auth/write")
    def mock_mcp_auth_write(
        request: Request, session: Session = Depends(get_db)
    ) -> dict[str, object]:
        ctx = resolve_auth_context(request, session, allow_cookie=False)
        if not ctx.is_authenticated:
            origin = resolve_public_origin(request, settings=custom_settings)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={
                    "WWW-Authenticate": build_www_authenticate_header(
                        origin, resource_path="/mcp/auth"
                    )
                },
            )
        require_scope(ctx, SCOPE_WRITE)
        return {"ok": True, "auth_method": ctx.auth_method}

    return app


def _extract_csrf(html_body: str) -> str:
    match = _CSRF_INPUT_RE.search(html_body)
    assert match is not None, "Expected hidden csrf_token input in HTML form"
    return match.group(1)


# -----------------------------------------------------------------------------
# 1. Schema, Migration & Tier C ODbL Invariants
# -----------------------------------------------------------------------------


def test_migration_0006_and_tier_c_orm_invariants() -> None:
    assert MIGRATION_0006_PATH.exists()
    spec = importlib.util.spec_from_file_location("migration_0006", MIGRATION_0006_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.revision == "0006_ai_mcp_auth"
    assert mod.down_revision == "0005_user_token_version"
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)

    # All four models must stay strictly Tier C (zero OSM columns or geometries)
    for model in (PersonalAccessToken, OAuthClient, OAuthAuthorizationCode, OAuthToken):
        table = model.__table__
        col_names = {c.name for c in table.columns}
        assert "geom" not in col_names
        assert "osm_way_id" not in col_names
        assert "tags" not in col_names
        for constraint in table.constraints:
            if isinstance(constraint, sa.CheckConstraint) and constraint.name:
                assert not constraint.name.startswith("ck_ck_")
                assert len(constraint.name) <= 60


# -----------------------------------------------------------------------------
# 2. Pure Unit Tests: Scopes, Redirect Validation, PKCE S256, CSRF, Metadata
# -----------------------------------------------------------------------------


def test_scope_normalization_and_auth_context_helpers() -> None:
    assert normalize_scopes([SCOPE_READ]) == [SCOPE_READ]
    # trailforge:write implies trailforge:read
    assert normalize_scopes([SCOPE_WRITE]) == [SCOPE_READ, SCOPE_WRITE]
    assert normalize_scopes("trailforge:write trailforge:read") == [SCOPE_READ, SCOPE_WRITE]

    with pytest.raises(ValueError):
        normalize_scopes([])
    with pytest.raises(ValueError):
        normalize_scopes(["admin:all"])

    fake_user = User(
        id=uuid.uuid4(),
        email="x@example.com",
        display_name="X",
        password_hash="stub",
        role="member",
        is_active=True,
        token_version=1,
    )
    read_ctx = McpAuthContext(user=fake_user, auth_method="pat", scopes=frozenset({SCOPE_READ}))
    assert read_ctx.is_authenticated is True
    assert read_ctx.can_read is True
    assert read_ctx.can_write is False
    require_scope(read_ctx, SCOPE_READ)
    with pytest.raises(HTTPException) as exc_info:
        require_scope(read_ctx, SCOPE_WRITE)
    assert exc_info.value.status_code == 403


@pytest.mark.parametrize(
    "valid_uri",
    [
        "https://chatgpt.com/connector_platform_oauth_redirect",
        "https://claude.ai/api/mcp/auth_callback",
        "https://app.example.com:8443/oauth/cb?mode=mcp",
        "http://localhost:8765/callback",
        "http://127.0.0.1:4321/cb",
        "http://[::1]:9000/oauth/callback",
    ],
)
def test_redirect_uri_accepts_safe_https_and_loopback(valid_uri: str) -> None:
    assert validate_redirect_uri(valid_uri) == valid_uri


@pytest.mark.parametrize(
    "bad_uri",
    [
        "",
        "   https://example.com/cb",
        "http://example.com/callback",
        "http://192.0.2.10:8080/callback",
        "http://localhost.evil.example/cb",
        "http://127.0.0.1.evil.example/cb",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "ftp://example.com/cb",
        "//evil.example/cb",
        "https://example.com/*",
        "https://*.example.com/cb",
        "https://user:pass@example.com/cb",
        "https://example.com/cb#fragment",
        "https://example.com/cb\\@evil.example",
        "https://example.com/cb\r\nX-Injected: 1",
        "https://0.0.0.0/cb",
    ],
)
def test_redirect_uri_rejects_open_redirects_and_unsafe_schemes(bad_uri: str) -> None:
    with pytest.raises(OAuthError) as exc_info:
        validate_redirect_uri(bad_uri)
    assert exc_info.value.error == "invalid_redirect_uri"


def test_pkce_s256_validation_and_verification() -> None:
    verifier = "aB3dE6gH9jK2mN5pQ8rS1tU4vW7xY0zA_bC-dE.fG~hI12345"
    challenge = compute_pkce_s256(verifier)
    assert len(challenge) == 43
    assert validate_code_challenge(challenge, "S256") == challenge
    assert verify_pkce_s256(verifier, challenge) is True
    assert verify_pkce_s256(verifier + "x", challenge) is False
    assert verify_pkce_s256("too-short", challenge) is False

    with pytest.raises(OAuthError):
        validate_code_challenge(challenge, "plain")
    with pytest.raises(OAuthError):
        validate_code_challenge("", "S256")


def test_csrf_nonce_binding_and_tamper_detection() -> None:
    nonce = create_oauth_csrf_nonce(
        session_binding="user:123:tv:1",
        action="consent",
        client_id="tf_cli_abc",
        redirect_uri="https://claude.ai/cb",
        code_challenge="a" * 43,
        state="xyz",
        scope="trailforge:read",
    )
    assert (
        verify_oauth_csrf_nonce(
            nonce,
            session_binding="user:123:tv:1",
            action="consent",
            client_id="tf_cli_abc",
            redirect_uri="https://claude.ai/cb",
            code_challenge="a" * 43,
            state="xyz",
            scope="trailforge:read",
        )
        is True
    )
    # Tampered scope or session binding rejected
    assert (
        verify_oauth_csrf_nonce(
            nonce,
            session_binding="user:123:tv:1",
            action="consent",
            client_id="tf_cli_abc",
            redirect_uri="https://claude.ai/cb",
            code_challenge="a" * 43,
            state="xyz",
            scope="trailforge:read trailforge:write",
        )
        is False
    )
    assert (
        verify_oauth_csrf_nonce(
            nonce,
            session_binding="user:999:tv:1",
            action="consent",
            client_id="tf_cli_abc",
            redirect_uri="https://claude.ai/cb",
            code_challenge="a" * 43,
            state="xyz",
            scope="trailforge:read",
        )
        is False
    )
    # Expired nonce rejected
    expired = create_oauth_csrf_nonce(
        session_binding="user:123:tv:1",
        action="consent",
        client_id="tf_cli_abc",
        redirect_uri="https://claude.ai/cb",
        code_challenge="a" * 43,
        state="xyz",
        scope="trailforge:read",
        ttl_seconds=-5,
    )
    assert (
        verify_oauth_csrf_nonce(
            expired,
            session_binding="user:123:tv:1",
            action="consent",
            client_id="tf_cli_abc",
            redirect_uri="https://claude.ai/cb",
            code_challenge="a" * 43,
            state="xyz",
            scope="trailforge:read",
        )
        is False
    )


def test_discovery_metadata_and_www_authenticate_format() -> None:
    origin = "https://trails.example.com"
    prm = build_protected_resource_metadata(origin, resource_path="/mcp/auth")
    assert prm["resource"] == "https://trails.example.com/mcp/auth"
    assert prm["authorization_servers"] == [origin]
    assert prm["bearer_methods_supported"] == ["header"]
    assert prm["scopes_supported"] == [SCOPE_READ, SCOPE_WRITE]

    asm = build_authorization_server_metadata(origin)
    assert asm["issuer"] == origin
    assert asm["authorization_endpoint"] == f"{origin}/api/oauth/authorize"
    assert asm["token_endpoint"] == f"{origin}/api/oauth/token"
    assert asm["registration_endpoint"] == f"{origin}/api/oauth/register"
    assert asm["revocation_endpoint"] == f"{origin}/api/oauth/revoke"
    assert asm["code_challenge_methods_supported"] == ["S256"]

    www_auth = build_www_authenticate_header(origin, resource_path="/mcp/auth")
    assert www_auth == (
        'Bearer resource_metadata="https://trails.example.com/.well-known/'
        'oauth-protected-resource/mcp/auth", scope="trailforge:read trailforge:write"'
    )


# -----------------------------------------------------------------------------
# 3. SC-6: Personal Access Tokens End-to-End Lifecycle
# -----------------------------------------------------------------------------


def test_pat_lifecycle_scopes_revocation_suspension_and_logout(sqlite_db: Session) -> None:
    user_a = _create_user(sqlite_db, email="alice@example.com", display_name="Alice")
    user_b = _create_user(sqlite_db, email="bob@example.com", display_name="Bob")

    session_a = create_access_token(
        str(user_a.id), user_a.email, token_version=user_a.token_version
    )
    session_b = create_access_token(
        str(user_b.id), user_b.email, token_version=user_b.token_version
    )

    app = _build_test_app(sqlite_db)
    with TestClient(app) as client:
        # Anonymous cannot list or mint PATs
        assert client.get("/api/auth/tokens").status_code == 401
        assert client.post("/api/auth/tokens", json={"name": "Nope"}).status_code == 401

        # Mint read-only PAT
        resp = client.post(
            "/api/auth/tokens",
            headers={"Authorization": f"Bearer {session_a}"},
            json={"name": "Claude Code RO", "scopes": [SCOPE_READ], "expires_in_days": 30},
        )
        assert resp.status_code == 201
        assert "no-store" in resp.headers.get("cache-control", "")
        body = resp.json()
        ro_token = body["token"]
        ro_id = body["id"]
        assert ro_token.startswith(PAT_PREFIX)
        assert body["token_prefix"] == ro_token[:13]
        assert body["scopes"] == [SCOPE_READ]

        # DB stores SHA-256 hash only (never plaintext)
        pat_row = sqlite_db.get(PersonalAccessToken, uuid.UUID(ro_id))
        assert pat_row is not None
        assert pat_row.token_hash == hash_token(ro_token)
        assert pat_row.token_hash != ro_token

        # Listing tokens never reveals plaintext or hash
        list_resp = client.get("/api/auth/tokens", headers={"Authorization": f"Bearer {session_a}"})
        assert list_resp.status_code == 200
        listed = list_resp.json()["tokens"]
        assert len(listed) == 1
        assert "token" not in listed[0]
        assert "token_hash" not in listed[0]
        assert listed[0]["token_prefix"] == ro_token[:13]

        # PAT CANNOT manage /api/auth/tokens or /api/auth/oauth-grants (403)
        pat_hdr = {"Authorization": f"Bearer {ro_token}"}
        assert client.get("/api/auth/tokens", headers=pat_hdr).status_code == 403
        assert (
            client.post(
                "/api/auth/tokens",
                headers={"Authorization": f"Bearer {ro_token}"},
                json={"name": "Escalate"},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/auth/oauth-grants", headers={"Authorization": f"Bearer {ro_token}"}
            ).status_code
            == 403
        )

        # Read-only PAT authenticates /mcp & /mcp/auth read, but is rejected (403) on writes
        r_mcp = client.get("/mcp", headers={"Authorization": f"Bearer {ro_token}"})
        assert r_mcp.status_code == 200
        assert r_mcp.json()["authenticated"] is True
        assert r_mcp.json()["auth_method"] == "pat"

        r_auth = client.get("/mcp/auth", headers={"Authorization": f"Bearer {ro_token}"})
        assert r_auth.status_code == 200

        r_write = client.post("/mcp/auth/write", headers={"Authorization": f"Bearer {ro_token}"})
        assert r_write.status_code == 403

        # Mint write-scoped PAT (implies read)
        rw_resp = client.post(
            "/api/auth/tokens",
            headers={"Authorization": f"Bearer {session_a}"},
            json={"name": "AGY RW", "scopes": [SCOPE_WRITE]},
        )
        assert rw_resp.status_code == 201
        rw_token = rw_resp.json()["token"]
        assert rw_resp.json()["scopes"] == [SCOPE_READ, SCOPE_WRITE]
        assert (
            client.post(
                "/mcp/auth/write", headers={"Authorization": f"Bearer {rw_token}"}
            ).status_code
            == 200
        )

        # Cross-user revocation denied (404, no leak)
        assert (
            client.delete(
                f"/api/auth/tokens/{ro_id}",
                headers={"Authorization": f"Bearer {session_b}"},
            ).status_code
            == 404
        )

        # Owner revokes read-only PAT -> stops working immediately
        del_resp = client.delete(
            f"/api/auth/tokens/{ro_id}",
            headers={"Authorization": f"Bearer {session_a}"},
        )
        assert del_resp.status_code == 200
        assert (
            client.get("/mcp/auth", headers={"Authorization": f"Bearer {ro_token}"}).status_code
            == 401
        )

        # Suspension immediately invalidates remaining active PAT
        user_a.is_active = False
        sqlite_db.commit()
        assert (
            client.get("/mcp/auth", headers={"Authorization": f"Bearer {rw_token}"}).status_code
            == 401
        )

        # Re-activate, then test logout (token_version bump) immediately invalidates PAT
        user_a.is_active = True
        sqlite_db.commit()
        assert (
            client.get("/mcp/auth", headers={"Authorization": f"Bearer {rw_token}"}).status_code
            == 200
        )

        logout_resp = client.post(
            "/api/auth/logout",
            headers={"Authorization": f"Bearer {session_a}"},
        )
        assert logout_resp.status_code == 200
        assert (
            client.get("/mcp/auth", headers={"Authorization": f"Bearer {rw_token}"}).status_code
            == 401
        )


def test_pat_expiry_and_active_limit_enforced(sqlite_db: Session) -> None:
    user = _create_user(sqlite_db, email="cap@example.com", display_name="Cap Test")
    now = datetime.now(UTC)

    created_exp = create_personal_access_token(
        sqlite_db,
        user,
        name="Short lived",
        scopes=[SCOPE_READ],
        expires_at=now + timedelta(seconds=30),
        now=now,
    )
    assert resolve_bearer_token(created_exp.plaintext_token, sqlite_db, now=now) is not None
    assert (
        resolve_bearer_token(
            created_exp.plaintext_token, sqlite_db, now=now + timedelta(seconds=60)
        )
        is None
    )

    # Fill up to MAX_ACTIVE_PATS_PER_USER (already 1 active at `now`)
    for idx in range(MAX_ACTIVE_PATS_PER_USER - 1):
        create_personal_access_token(sqlite_db, user, name=f"PAT {idx}", now=now)

    with pytest.raises(TokenLimitExceededError):
        create_personal_access_token(sqlite_db, user, name="Over Limit", now=now)


# -----------------------------------------------------------------------------
# 4. SC-7: Full OAuth 2.1 + PKCE S256 + DCR + Rotation + Reuse + Revoke Flow
# -----------------------------------------------------------------------------


def test_oauth_discovery_cors_and_feature_toggles(
    sqlite_db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRAILFORGE_PUBLIC_BASE_URL", "https://trails.example.com")
    app = _build_test_app(sqlite_db)

    with TestClient(app) as client:
        for path, expected_resource in (
            ("/.well-known/oauth-protected-resource", "https://trails.example.com/mcp"),
            ("/.well-known/oauth-protected-resource/mcp", "https://trails.example.com/mcp"),
            (
                "/.well-known/oauth-protected-resource/mcp/auth",
                "https://trails.example.com/mcp/auth",
            ),
        ):
            r = client.get(path)
            assert r.status_code == 200
            assert r.json()["resource"] == expected_resource
            assert r.headers["access-control-allow-origin"] == "*"
            assert "WWW-Authenticate" in r.headers["access-control-expose-headers"]
            assert "Mcp-Session-Id" in r.headers["access-control-expose-headers"]

            opt = client.options(path)
            assert opt.status_code == 204
            assert opt.headers["access-control-allow-origin"] == "*"

        asm = client.get("/.well-known/oauth-authorization-server")
        assert asm.status_code == 200
        assert asm.json()["issuer"] == "https://trails.example.com"
        assert asm.json()["code_challenge_methods_supported"] == ["S256"]

        # Anonymous /mcp/auth returns 401 with exact RFC 9728 WWW-Authenticate header
        unauth = client.get("/mcp/auth")
        assert unauth.status_code == 401
        assert unauth.headers["www-authenticate"] == (
            'Bearer resource_metadata="https://trails.example.com/.well-known/'
            'oauth-protected-resource/mcp/auth", scope="trailforge:read trailforge:write"'
        )

    # anonymous_only hides /mcp/auth discovery and OAuth endpoints
    monkeypatch.setenv("TRAILFORGE_MCP_AUTH_MODE", "anonymous_only")
    with TestClient(app) as client:
        assert client.get("/.well-known/oauth-protected-resource").status_code == 200
        assert client.get("/.well-known/oauth-protected-resource/mcp/auth").status_code == 404
        assert client.get("/.well-known/oauth-authorization-server").status_code == 404
        assert (
            client.post(
                "/api/oauth/register",
                json={"client_name": "X", "redirect_uris": ["https://claude.ai/cb"]},
            ).status_code
            == 404
        )

    # TRAILFORGE_ENABLE_MCP=false disables all discovery & OAuth routes (404)
    monkeypatch.setenv("TRAILFORGE_MCP_AUTH_MODE", "hybrid")
    monkeypatch.setenv("TRAILFORGE_ENABLE_MCP", "false")
    with TestClient(app) as client:
        assert client.get("/.well-known/oauth-protected-resource").status_code == 404
        assert client.get("/.well-known/oauth-authorization-server").status_code == 404


def test_full_oauth21_pkce_dcr_rotation_reuse_revoke_and_grants(
    sqlite_db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRAILFORGE_PUBLIC_BASE_URL", "https://trails.example.com")
    user = _create_user(
        sqlite_db,
        email="explorer@example.com",
        display_name="High Country Explorer",
        password="SnowyMountains4WD!",
    )
    other_user = _create_user(sqlite_db, email="other@example.com", display_name="Other")
    other_session = create_access_token(
        str(other_user.id), other_user.email, token_version=other_user.token_version
    )

    app = _build_test_app(sqlite_db)
    with TestClient(app, follow_redirects=False) as client:
        # 1. DCR rejects unsafe redirect URIs
        bad_reg = client.post(
            "/api/oauth/register",
            json={
                "client_name": "Evil App",
                "redirect_uris": ["javascript:alert(1)"],
            },
        )
        assert bad_reg.status_code == 400
        assert bad_reg.json()["error"] == "invalid_redirect_uri"

        # 2. DCR registers public PKCE client (with XSS payload in name to verify HTML escaping!)
        redirect_uri = "https://claude.ai/api/mcp/auth_callback"
        reg_resp = client.post(
            "/api/oauth/register",
            json={
                "client_name": "Claude <script>alert(1)</script>",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert reg_resp.status_code == 201
        reg_data = reg_resp.json()
        client_id = reg_data["client_id"]
        assert client_id.startswith("tf_cli_")
        assert "client_secret" not in reg_data

        # 3. PKCE S256 verifier & challenge
        code_verifier = "TrailForge_PKCE_Verifier_HighCountry_2026_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        code_challenge = compute_pkce_s256(code_verifier)
        state = "csrf-state-xyz-123"

        # 4. Reject authorize without PKCE S256 or with unregistered redirect_uri
        assert (
            client.get(
                "/api/oauth/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": "trailforge:read trailforge:write",
                    "code_challenge": code_challenge,
                    "code_challenge_method": "plain",
                },
            ).status_code
            == 400
        )
        assert (
            client.get(
                "/api/oauth/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": "https://claude.ai/api/mcp/auth_callback?extra=1",
                    "response_type": "code",
                    "scope": "trailforge:read",
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                },
            ).status_code
            == 400
        )

        # 5. Unauthenticated GET /api/oauth/authorize renders auto-escaped inline login form
        auth_params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "trailforge:read trailforge:write",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "resource": "https://trails.example.com/mcp/auth",
        }
        login_page = client.get("/api/oauth/authorize", params=auth_params)
        assert login_page.status_code == 200
        assert login_page.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in login_page.headers["content-security-policy"]
        assert "form-action 'self';" in login_page.headers["content-security-policy"]
        assert "<script>alert(1)</script>" not in login_page.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in login_page.text
        login_csrf = _extract_csrf(login_page.text)

        # 6. Inline login with wrong password returns 401
        bad_login = client.post(
            "/api/oauth/authorize",
            data={
                **auth_params,
                "action": "login",
                "csrf_token": login_csrf,
                "email": user.email,
                "password": "WrongPassword99!",
            },
        )
        assert bad_login.status_code == 401

        # 7. Inline login with valid password sets tf_session cookie & renders consent HTML
        consent_page = client.post(
            "/api/oauth/authorize",
            data={
                **auth_params,
                "action": "login",
                "csrf_token": login_csrf,
                "email": user.email,
                "password": "SnowyMountains4WD!",
            },
        )
        assert consent_page.status_code == 200
        assert consent_page.headers["x-frame-options"] == "DENY"
        consent_csp = consent_page.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in consent_csp
        assert "form-action 'self' https://claude.ai;" in consent_csp
        assert user.email in consent_page.text
        assert "Read your rigs, saved routes &amp; trip summaries" in consent_page.text
        assert "Save routes, pins &amp; favorites" in consent_page.text
        assert "<script>alert(1)</script>" not in consent_page.text
        consent_csrf = _extract_csrf(consent_page.text)

        # Signed-in GET /api/oauth/authorize also includes redirect origin in CSP form-action
        signed_in_get = client.get("/api/oauth/authorize", params=auth_params)
        assert signed_in_get.status_code == 200
        assert signed_in_get.headers["x-frame-options"] == "DENY"
        signed_in_csp = signed_in_get.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in signed_in_csp
        assert "form-action 'self' https://claude.ai;" in signed_in_csp

        # 8. Tampered CSRF token on consent POST rejected (403)
        tampered_post = client.post(
            "/api/oauth/authorize",
            data={
                **auth_params,
                "action": "consent",
                "decision": "approve",
                "csrf_token": consent_csrf + "tamper",
            },
        )
        assert tampered_post.status_code == 403

        # 9. Deny consent redirects with error=access_denied
        deny_resp = client.post(
            "/api/oauth/authorize",
            data={
                **auth_params,
                "action": "consent",
                "decision": "deny",
                "csrf_token": consent_csrf,
            },
        )
        assert deny_resp.status_code == 302
        deny_q = parse_qs(urlsplit(deny_resp.headers["location"]).query)
        assert deny_q["error"] == ["access_denied"]
        assert deny_q["state"] == [state]

        # 10. Approve consent redirects with single-use code (stored hashed only)
        approve_resp = client.post(
            "/api/oauth/authorize",
            data={
                **auth_params,
                "action": "consent",
                "decision": "approve",
                "csrf_token": consent_csrf,
            },
        )
        assert approve_resp.status_code == 302
        approve_q = parse_qs(urlsplit(approve_resp.headers["location"]).query)
        auth_code = approve_q["code"][0]
        assert auth_code.startswith(OAUTH_CODE_PREFIX)
        assert approve_q["state"] == [state]

        code_row = sqlite_db.get(OAuthAuthorizationCode, hash_token(auth_code))
        assert code_row is not None
        assert code_row.consumed_at is None

        # 11. Token exchange rejects wrong PKCE verifier
        bad_pkce = client.post(
            "/api/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": "WrongVerifier_" + ("x" * 40),
            },
        )
        assert bad_pkce.status_code == 400
        assert bad_pkce.json()["error"] == "invalid_grant"

        # 12. Valid token exchange issues access + refresh tokens (stored hashed only)
        tok_resp = client.post(
            "/api/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
        )
        assert tok_resp.status_code == 200
        assert "no-store" in tok_resp.headers.get("cache-control", "")
        tok1 = tok_resp.json()
        access_1 = tok1["access_token"]
        refresh_1 = tok1["refresh_token"]
        assert access_1.startswith(OAUTH_ACCESS_PREFIX)
        assert refresh_1.startswith(OAUTH_REFRESH_PREFIX)
        assert tok1["token_type"] == "Bearer"
        assert tok1["scope"] == "trailforge:read trailforge:write"

        # 13. Replaying single-use auth code is rejected
        replay_code = client.post(
            "/api/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
        )
        assert replay_code.status_code == 400
        assert replay_code.json()["error"] == "invalid_grant"

        # 14. OAuth access token authenticates /mcp/auth read & write, but NOT /api/auth/tokens
        r_mcp = client.get("/mcp/auth", headers={"Authorization": f"Bearer {access_1}"})
        assert r_mcp.status_code == 200
        assert r_mcp.json()["auth_method"] == "oauth"
        assert r_mcp.json()["client_id"] == client_id
        oauth_hdr = {"Authorization": f"Bearer {access_1}"}
        assert client.post("/mcp/auth/write", headers=oauth_hdr).status_code == 200
        assert (
            client.get(
                "/api/auth/tokens", headers={"Authorization": f"Bearer {access_1}"}
            ).status_code
            == 403
        )

        # 15. Account Panel /api/auth/oauth-grants lists the connected app (using session cookie!)
        grants_resp = client.get("/api/auth/oauth-grants")
        assert grants_resp.status_code == 200
        grants = grants_resp.json()["grants"]
        assert len(grants) == 1
        assert grants[0]["client_id"] == client_id
        assert grants[0]["scopes"] == [SCOPE_READ, SCOPE_WRITE]

        # Another user cannot revoke this user's grant
        assert (
            client.delete(
                f"/api/auth/oauth-grants/{client_id}",
                headers={"Authorization": f"Bearer {other_session}"},
            ).status_code
            == 404
        )

        # 16. Refresh token rotation: mints access_2 + refresh_2, revokes access_1 + refresh_1
        # Also verify refresh scope downscoping works while escalation is rejected
        rot_resp = client.post(
            "/api/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_1,
                "client_id": client_id,
                "scope": SCOPE_READ,
            },
        )
        assert rot_resp.status_code == 200
        tok2 = rot_resp.json()
        access_2 = tok2["access_token"]
        refresh_2 = tok2["refresh_token"]
        assert access_2 != access_1
        assert refresh_2 != refresh_1
        assert tok2["scope"] == SCOPE_READ

        # Downscoped access_2 can read, cannot write; escalating refresh scope is rejected
        assert (
            client.get("/mcp/auth", headers={"Authorization": f"Bearer {access_2}"}).status_code
            == 200
        )
        assert (
            client.post(
                "/mcp/auth/write", headers={"Authorization": f"Bearer {access_2}"}
            ).status_code
            == 403
        )
        escalate_resp = client.post(
            "/api/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_2,
                "client_id": client_id,
                "scope": "trailforge:read trailforge:write",
            },
        )
        assert escalate_resp.status_code == 400
        assert escalate_resp.json()["error"] == "invalid_scope"

        # Old access_1 is revoked; new access_2 still works
        assert (
            client.get("/mcp/auth", headers={"Authorization": f"Bearer {access_1}"}).status_code
            == 401
        )
        assert (
            client.get("/mcp/auth", headers={"Authorization": f"Bearer {access_2}"}).status_code
            == 200
        )

        # 17. REFRESH TOKEN REUSE DETECTION: replaying refresh_1 revokes ENTIRE token family!
        reuse_resp = client.post(
            "/api/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_1,
                "client_id": client_id,
            },
        )
        assert reuse_resp.status_code == 400
        assert reuse_resp.json()["error"] == "invalid_grant"

        # Now access_2 AND refresh_2 are immediately revoked!
        assert (
            client.get("/mcp/auth", headers={"Authorization": f"Bearer {access_2}"}).status_code
            == 401
        )
        assert (
            client.post(
                "/api/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_2,
                    "client_id": client_id,
                },
            ).status_code
            == 400
        )

        # 18. Mint a fresh grant, then test RFC 7009 revoke AND Account Panel grant revoke
        approve_2 = client.post(
            "/api/oauth/authorize",
            data={
                **auth_params,
                "action": "consent",
                "decision": "approve",
                "csrf_token": consent_csrf,
            },
        )
        code_2 = parse_qs(urlsplit(approve_2.headers["location"]).query)["code"][0]
        tok3 = client.post(
            "/api/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code_2,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
        ).json()
        assert (
            client.get(
                "/mcp/auth", headers={"Authorization": f"Bearer {tok3['access_token']}"}
            ).status_code
            == 200
        )

        # RFC 7009 revoke refresh token revokes its family; unknown token still returns 200
        rev = client.post(
            "/api/oauth/revoke",
            data={
                "token": tok3["refresh_token"],
                "token_type_hint": "refresh_token",
                "client_id": client_id,
            },
        )
        assert rev.status_code == 200
        assert (
            client.get(
                "/mcp/auth", headers={"Authorization": f"Bearer {tok3['access_token']}"}
            ).status_code
            == 401
        )
        assert (
            client.post("/api/oauth/revoke", data={"token": "tf_oat_does_not_exist"}).status_code
            == 200
        )

        # Re-grant and revoke via Account Panel DELETE /api/auth/oauth-grants/{client_id}
        approve_3 = client.post(
            "/api/oauth/authorize",
            data={
                **auth_params,
                "action": "consent",
                "decision": "approve",
                "csrf_token": consent_csrf,
            },
        )
        code_3 = parse_qs(urlsplit(approve_3.headers["location"]).query)["code"][0]
        tok4 = client.post(
            "/api/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code_3,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
        ).json()
        assert (
            client.get(
                "/mcp/auth", headers={"Authorization": f"Bearer {tok4['access_token']}"}
            ).status_code
            == 200
        )

        del_grant = client.delete(f"/api/auth/oauth-grants/{client_id}")
        assert del_grant.status_code == 200
        assert del_grant.json()["revoked_tokens"] >= 1
        assert (
            client.get(
                "/mcp/auth", headers={"Authorization": f"Bearer {tok4['access_token']}"}
            ).status_code
            == 401
        )


def test_confidential_client_expired_code_and_dcr_bounds(
    sqlite_db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _create_user(sqlite_db, email="conf@example.com", display_name="Conf User")
    monkeypatch.setenv("TRAILFORGE_OAUTH_DCR_MAX_PER_IP_PER_DAY", "2")
    monkeypatch.setenv("TRAILFORGE_OAUTH_MAX_CLIENTS", "3")

    now = datetime.now(UTC)

    # Register confidential client (`client_secret_post`)
    reg = register_oauth_client(
        sqlite_db,
        client_name="Confidential MCP Client",
        redirect_uris=["http://127.0.0.1:8765/callback"],
        token_endpoint_auth_method="client_secret_post",
        client_ip="192.0.2.50",
        now=now,
    )
    assert reg.client_secret is not None
    assert reg.client.client_secret_hash == hash_token(reg.client_secret)
    assert reg.client.registered_by_ip_hash != "192.0.2.50"

    # Second registration from same IP succeeds (cap=2)
    register_oauth_client(
        sqlite_db,
        client_name="Second Client",
        redirect_uris=["http://localhost:8765/cb"],
        client_ip="192.0.2.50",
        now=now,
    )
    # Third from same IP hits per-IP daily cap -> 429
    with pytest.raises(OAuthError) as rate_exc:
        register_oauth_client(
            sqlite_db,
            client_name="Third Client",
            redirect_uris=["http://localhost:8765/cb"],
            client_ip="192.0.2.50",
            now=now,
        )
    assert rate_exc.value.status_code == 429

    # Different IP fills global cap (max=3)
    old_reg = register_oauth_client(
        sqlite_db,
        client_name="Old Unused Client",
        redirect_uris=["http://[::1]:8765/cb"],
        client_ip="192.0.2.51",
        now=now - timedelta(days=120),
    )
    assert old_reg.client.client_id is not None

    # Pruning removes the 120-day-old unused client so a fresh registration fits under cap=3
    pruned = prune_stale_oauth_clients(sqlite_db, now=now, unused_ttl_days=90)
    assert pruned == 1
    sqlite_db.commit()

    register_oauth_client(
        sqlite_db,
        client_name="Fresh Client",
        redirect_uris=["https://chatgpt.com/cb"],
        client_ip="192.0.2.52",
        now=now,
    )
    # Now 3 fresh clients exist -> 4th hits table capacity (503)
    with pytest.raises(OAuthError) as cap_exc:
        register_oauth_client(
            sqlite_db,
            client_name="Overflow Client",
            redirect_uris=["https://chatgpt.com/cb"],
            client_ip="192.0.2.53",
            now=now,
        )
    assert cap_exc.value.status_code == 503

    # Test confidential client auth + 60 s auth code expiry
    verifier = "ConfidentialVerifier_1234567890_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    challenge = compute_pkce_s256(verifier)
    params = validate_authorize_request(
        sqlite_db,
        client_id=reg.client.client_id,
        redirect_uri="http://127.0.0.1:8765/callback",
        response_type="code",
        scope=SCOPE_READ,
        code_challenge=challenge,
        code_challenge_method="S256",
    )
    code = issue_authorization_code(sqlite_db, params=params, user=user, now=now)

    # Missing or wrong client_secret rejected (401 invalid_client)
    with pytest.raises(OAuthError) as bad_secret:
        exchange_authorization_code(
            sqlite_db,
            code=code,
            redirect_uri="http://127.0.0.1:8765/callback",
            client_id=reg.client.client_id,
            code_verifier=verifier,
            client_secret="wrong",
            now=now + timedelta(seconds=5),
        )
    assert bad_secret.value.status_code == 401
    assert bad_secret.value.error == "invalid_client"

    # Expired code (> 60 s) rejected (400 invalid_grant)
    with pytest.raises(OAuthError) as exp_exc:
        exchange_authorization_code(
            sqlite_db,
            code=code,
            redirect_uri="http://127.0.0.1:8765/callback",
            client_id=reg.client.client_id,
            code_verifier=verifier,
            client_secret=reg.client_secret,
            now=now + timedelta(seconds=61),
        )
    assert exp_exc.value.error == "invalid_grant"

    # Fresh code with valid secret succeeds
    code_ok = issue_authorization_code(sqlite_db, params=params, user=user, now=now)
    tok = exchange_authorization_code(
        sqlite_db,
        code=code_ok,
        redirect_uri="http://127.0.0.1:8765/callback",
        client_id=reg.client.client_id,
        code_verifier=verifier,
        client_secret=reg.client_secret,
        now=now + timedelta(seconds=10),
    )
    assert tok.access_token.startswith(OAUTH_ACCESS_PREFIX)
    ctx = resolve_bearer_token(tok.access_token, sqlite_db, now=now + timedelta(seconds=15))
    assert ctx is not None
    assert ctx.auth_method == "oauth"
    assert ctx.can_read is True
    assert ctx.can_write is False

    # Logout (token_version bump) invalidates OAuth access & refresh tokens immediately
    user.token_version += 1
    sqlite_db.commit()
    after_logout = now + timedelta(seconds=20)
    assert resolve_bearer_token(tok.access_token, sqlite_db, now=after_logout) is None


def test_public_origin_proxy_header_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRAILFORGE_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)

    app = FastAPI()
    app.include_router(well_known_router)

    with TestClient(app) as client:
        r = client.get(
            "/.well-known/oauth-authorization-server",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "trails.example.org",
            },
        )
        assert r.status_code == 200
        assert r.json()["issuer"] == "https://trails.example.org"

    # When trust_proxy_headers=False, ignore spoofed forwarded host
    monkeypatch.setenv("TRAILFORGE_TRUST_PROXY_HEADERS", "false")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            r2 = client.get(
                "/.well-known/oauth-authorization-server",
                headers={
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "spoofed.evil.example",
                },
            )
            assert r2.status_code == 200
            assert "spoofed.evil.example" not in r2.json()["issuer"]
    finally:
        get_settings.cache_clear()


# -----------------------------------------------------------------------------
# 5. Optional Live PostGIS Migration 0006 Verification (skips if DB unavailable)
# -----------------------------------------------------------------------------


@pytest.mark.integration
def test_migration_0006_tables_and_crud_on_postgis(postgis_engine: Engine) -> None:
    inspector = inspect(postgis_engine)
    tables = set(inspector.get_table_names(schema="public"))
    expected = {
        "personal_access_tokens",
        "oauth_clients",
        "oauth_authorization_codes",
        "oauth_tokens",
    }
    assert expected.issubset(tables), f"Missing AI auth tables: {expected - tables}"

    for tbl in expected:
        cols = {c["name"] for c in inspector.get_columns(tbl)}
        assert "geom" not in cols
        assert "osm_way_id" not in cols
        assert "tags" not in cols

    # Verify CHECK constraints follow single-prefix convention and <= 60 chars
    with postgis_engine.connect() as conn:
        bad_ck = conn.execute(
            text(
                r"""
                SELECT conname
                FROM pg_constraint
                WHERE connamespace = 'public'::regnamespace
                  AND contype = 'c'
                  AND conrelid != 'spatial_ref_sys'::regclass
                  AND (conname LIKE 'ck\_%\_ck\_%' ESCAPE '\' OR length(conname) > 60)
                """
            )
        ).scalars().all()
    assert not bad_ck, f"Malformed/double-prefixed CHECK constraints: {bad_ck}"

    with Session(postgis_engine) as session:
        user = User(
            email=f"pg-oauth-{uuid.uuid4().hex[:8]}@example.com",
            display_name="PG Tester",
            password_hash=hash_password("PostGIS4WD!"),
            role="member",
            is_active=True,
        )
        session.add(user)
        session.commit()
        try:
            created = create_personal_access_token(
                session, user, name="PG PAT", scopes=[SCOPE_READ, SCOPE_WRITE]
            )
            ctx = resolve_bearer_token(created.plaintext_token, session)
            assert ctx is not None
            assert ctx.user is not None and ctx.user.id == user.id
            assert ctx.can_write is True

            reg = register_oauth_client(
                session,
                client_name="PG Client",
                redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
                client_ip="192.0.2.77",
            )
            stored_client = session.execute(
                select(OAuthClient).where(OAuthClient.client_id == reg.client.client_id)
            ).scalar_one()
            assert stored_client.client_name == "PG Client"
            session.delete(stored_client)
        finally:
            session.delete(user)
            session.commit()
