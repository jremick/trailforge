"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Final, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_DEFAULT_AUTH_SECRET_KEY: Final[str] = "trailforge-dev-secret-change-in-production"
DEV_DEFAULT_PRIVACY_SECRET_KEY: Final[str] = "trailforge-dev-privacy-secret-change-in-production"


class Settings(BaseSettings):
    """Runtime settings for TrailForge API, worker, and migrations."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+psycopg://trailforge:trailforge@localhost:5432/trailforge",
        description="SQLAlchemy PostgreSQL/PostGIS connection URL.",
    )
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = Field(default="INFO")
    cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        description="Comma-separated list of allowed CORS origins.",
    )

    # Worker & privacy guardrails (locked v1 defaults: >= 250 m per SPEC_GIS_SCHEMA Sec 4.2)
    worker_poll_seconds: int = Field(default=30, ge=1)
    privacy_trim_meters: int = Field(default=250, ge=250, le=5000)
    snow_line_default_m: int = Field(default=1000, ge=0)

    # Pluggable map-matcher backend & optional Valhalla sidecar (Phase B v1.1.0)
    matcher_backend: Literal["postgis_hmm", "valhalla"] = Field(
        default="postgis_hmm",
        validation_alias=AliasChoices(
            "TRAILFORGE_MATCHER_BACKEND",
            "MATCHER_BACKEND",
            "matcher_backend",
        ),
        description="Active TrackMatcher backend ('postgis_hmm' default or optional 'valhalla').",
    )
    valhalla_url: str = Field(
        default="http://valhalla:8002",
        validation_alias=AliasChoices("VALHALLA_URL", "TRAILFORGE_VALHALLA_URL", "valhalla_url"),
        description="Base URL of optional Valhalla map-matching sidecar.",
    )
    valhalla_timeout_s: float = Field(
        default=10.0,
        gt=0.0,
        le=120.0,
        validation_alias=AliasChoices(
            "VALHALLA_TIMEOUT_S",
            "TRAILFORGE_VALHALLA_TIMEOUT_S",
            "valhalla_timeout_s",
        ),
        description="HTTP timeout in seconds for Valhalla /trace_attributes requests.",
    )
    valhalla_fallback_to_hmm: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "VALHALLA_FALLBACK_TO_HMM",
            "TRAILFORGE_VALHALLA_FALLBACK_TO_HMM",
            "valhalla_fallback_to_hmm",
        ),
        description="Gracefully fall back to PostGISHMMMatcher when Valhalla is unreachable.",
    )

    # Pan-Australia state scoping for live closure feeds & polling (Phase B v1.1.0)
    enabled_states: str = Field(
        default="VIC,NSW",
        validation_alias=AliasChoices(
            "TRAILFORGE_ENABLED_STATES",
            "ENABLED_STATES",
            "enabled_states",
        ),
        description=(
            "Comma-separated Australian state codes enabled for default feed polling "
            "('VIC,NSW' default; set 'VIC,NSW,ACT' or 'ALL' for pan-Australia)."
        ),
    )

    # Auth & privacy secrets (self-hosted, zero external IdP)
    auth_secret_key: str = Field(
        default=DEV_DEFAULT_AUTH_SECRET_KEY,
        validation_alias=AliasChoices("AUTH_SECRET_KEY", "TRAILFORGE_AUTH_SECRET_KEY"),
    )
    privacy_secret_key: str = Field(
        default=DEV_DEFAULT_PRIVACY_SECRET_KEY,
        validation_alias=AliasChoices("TRAILFORGE_PRIVACY_SECRET", "PRIVACY_SECRET_KEY"),
        description="HMAC secret used to derive revocable telemetry contribution hashes.",
    )
    auth_token_ttl_seconds: int = Field(default=60 * 60 * 24 * 14, ge=300)
    auth_cookie_name: str = Field(default="tf_session")
    auth_cookie_secure: bool = Field(default=False)

    # Local storage directories (streaming pack/tile/upload outputs)
    trailforge_data_dir: str = Field(default="/data")
    tiles_dir: str = Field(
        default="/data/tiles",
        validation_alias=AliasChoices("TILES_DIR", "TRAILFORGE_TILES_DIR", "tiles_dir"),
        description="Directory storing generated vector/terrain PMTiles and manifest.json.",
    )
    uploads_dir: str = Field(
        default="/data/uploads",
        validation_alias=AliasChoices("TRAILFORGE_UPLOADS_DIR", "UPLOADS_DIR", "uploads_dir"),
        description="Directory storing validated community condition report photo attachments.",
    )
    packs_dir: str = Field(
        default="/data/packs",
        validation_alias=AliasChoices("PACKS_DIR", "TRAILFORGE_PACKS_DIR", "packs_dir"),
        description="Directory storing built offline regional pack bundles and manifests.",
    )

    # Public instance access control & demo UX (v1.1.0 Public Hardening)
    registration_mode: Literal["open", "invite_only", "closed"] = Field(
        default="open",
        validation_alias=AliasChoices(
            "TRAILFORGE_REGISTRATION_MODE",
            "REGISTRATION_MODE",
            "registration_mode",
        ),
        description="Account registration policy ('open', 'invite_only', or 'closed').",
    )
    demo_banner: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRAILFORGE_DEMO_BANNER",
            "DEMO_BANNER",
            "demo_banner",
        ),
        description="Optional public info banner text displayed in the web UI.",
    )
    show_stack_telemetry_public: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRAILFORGE_SHOW_STACK_TELEMETRY_PUBLIC",
            "SHOW_STACK_TELEMETRY_PUBLIC",
            "show_stack_telemetry_public",
        ),
        description=(
            "Show the sidebar Stack & Offline Telemetry section to anonymous visitors "
            "(always visible to signed-in members and admins)."
        ),
    )

    # Optional Australian place-name geocoder fallback for /api/search (Nominatim AU)
    enable_geocoder: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRAILFORGE_ENABLE_GEOCODER",
            "ENABLE_GEOCODER",
            "enable_geocoder",
        ),
        description=(
            "Fall back to rate-limited Nominatim AU place search when local "
            "PostGIS search has few matches."
        ),
    )
    geocoder_url: str = Field(
        default="https://nominatim.openstreetmap.org/search",
        validation_alias=AliasChoices(
            "TRAILFORGE_GEOCODER_URL",
            "GEOCODER_URL",
            "geocoder_url",
        ),
        description="Nominatim search endpoint URL for Australian place-name fallback.",
    )
    geocoder_timeout_s: float = Field(
        default=3.5,
        gt=0.2,
        le=15.0,
        validation_alias=AliasChoices(
            "TRAILFORGE_GEOCODER_TIMEOUT_S",
            "GEOCODER_TIMEOUT_S",
            "geocoder_timeout_s",
        ),
        description="HTTP timeout in seconds for AU place-name geocoder fallback.",
    )

    # Edge rate limiting & storage quotas (zero Redis, bounded in-memory sliding windows)
    rate_limit_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRAILFORGE_RATE_LIMIT_ENABLED",
            "RATE_LIMIT_ENABLED",
            "rate_limit_enabled",
        ),
        description="Enable in-process sliding-window rate limiting and upload quotas.",
    )
    rate_limit_read_per_min: int = Field(
        default=180,
        ge=10,
        validation_alias=AliasChoices(
            "TRAILFORGE_RATE_LIMIT_READ_PER_MIN",
            "RATE_LIMIT_READ_PER_MIN",
            "rate_limit_read_per_min",
        ),
        description="Per-IP requests per minute allowed on general /api/* routes.",
    )
    rate_limit_auth_per_min: int = Field(
        default=12,
        ge=3,
        validation_alias=AliasChoices(
            "TRAILFORGE_RATE_LIMIT_AUTH_PER_MIN",
            "RATE_LIMIT_AUTH_PER_MIN",
            "rate_limit_auth_per_min",
        ),
        description="Per-IP requests per minute allowed on /api/auth/login and /register.",
    )
    rate_limit_upload_per_day: int = Field(
        default=25,
        ge=1,
        validation_alias=AliasChoices(
            "TRAILFORGE_RATE_LIMIT_UPLOAD_PER_DAY",
            "RATE_LIMIT_UPLOAD_PER_DAY",
            "rate_limit_upload_per_day",
        ),
        description="Maximum trip uploads / mobile syncs per non-admin member per 24 hours.",
    )
    uploads_max_total_mb: int = Field(
        default=500,
        ge=10,
        validation_alias=AliasChoices(
            "TRAILFORGE_UPLOADS_MAX_TOTAL_MB",
            "UPLOADS_MAX_TOTAL_MB",
            "uploads_max_total_mb",
        ),
        description="Global disk quota in MiB for condition report photo attachments.",
    )
    db_statement_timeout_ms: int = Field(
        default=15_000,
        ge=0,
        validation_alias=AliasChoices(
            "TRAILFORGE_DB_STATEMENT_TIMEOUT_MS",
            "DB_STATEMENT_TIMEOUT_MS",
            "db_statement_timeout_ms",
        ),
        description=(
            "PostgreSQL statement_timeout (ms) enforced on FastAPI request sessions "
            "(0 disables; background workers and CLI pipelines are unaffected)."
        ),
    )
    trust_proxy_headers: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRAILFORGE_TRUST_PROXY_HEADERS",
            "TRUST_PROXY_HEADERS",
            "trust_proxy_headers",
        ),
        description=(
            "Trust X-Forwarded-For / X-Forwarded-Host proxy headers when requests arrive from "
            "a peer matching TRAILFORGE_TRUSTED_PROXY_CIDRS."
        ),
    )
    trusted_proxy_cidrs: str = Field(
        default="127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7",
        validation_alias=AliasChoices(
            "TRAILFORGE_TRUSTED_PROXY_CIDRS",
            "TRUSTED_PROXY_CIDRS",
            "trusted_proxy_cidrs",
        ),
        description=(
            "Comma-separated CIDR ranges of reverse proxies allowed to supply X-Forwarded-* "
            "headers when TRAILFORGE_TRUST_PROXY_HEADERS=true."
        ),
    )
    trust_cloudflare_headers: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRAILFORGE_TRUST_CLOUDFLARE_HEADERS",
            "TRUST_CLOUDFLARE_HEADERS",
            "trust_cloudflare_headers",
        ),
        description=(
            "Explicitly trust CF-Connecting-IP from a trusted proxy peer (automatically enabled "
            "when TRAILFORGE_ORIGIN_SHARED_SECRET is set)."
        ),
    )
    origin_shared_secret: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRAILFORGE_ORIGIN_SHARED_SECRET",
            "ORIGIN_SHARED_SECRET",
            "origin_shared_secret",
        ),
        description=(
            "Optional shared secret required via X-TrailForge-Origin-Secret from the "
            "Cloudflare Pages/Worker proxy to block direct origin bypass."
        ),
    )
    enable_docs: bool | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "TRAILFORGE_ENABLE_DOCS",
            "ENABLE_DOCS",
            "enable_docs",
        ),
        description=(
            "Explicitly enable or disable OpenAPI /docs, /redoc, and /openapi.json "
            "(defaults to enabled in local dev, disabled in production mode)."
        ),
    )

    @field_validator("enable_docs", mode="before")
    @classmethod
    def _coerce_blank_enable_docs(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # Cloudflare R2 / CDN static offload & pure-Python SigV4 sync
    tiles_public_base_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TILES_PUBLIC_BASE_URL",
            "TRAILFORGE_TILES_PUBLIC_BASE_URL",
            "tiles_public_base_url",
        ),
        description="Optional public CDN/R2 base URL serving PMTiles archives.",
    )
    packs_public_base_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "PACKS_PUBLIC_BASE_URL",
            "TRAILFORGE_PACKS_PUBLIC_BASE_URL",
            "packs_public_base_url",
        ),
        description="Optional public CDN/R2 base URL serving offline regional packs.",
    )
    vector_basemap_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRAILFORGE_VECTOR_BASEMAP_URL",
            "VECTOR_BASEMAP_URL",
            "vector_basemap_url",
        ),
        description=(
            "Optional self-hosted OpenMapTiles vector basemap URL (.pmtiles archive or TileJSON); "
            "leave empty to use zero-key OpenFreeMap online."
        ),
    )
    r2_endpoint_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRAILFORGE_R2_ENDPOINT_URL",
            "R2_ENDPOINT_URL",
            "r2_endpoint_url",
        ),
        description="Cloudflare R2 S3-compatible endpoint URL (e.g. https://<acct>.r2.cloudflarestorage.com).",
    )
    r2_bucket: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRAILFORGE_R2_BUCKET",
            "R2_BUCKET",
            "r2_bucket",
        ),
        description="Cloudflare R2 target bucket name for tile/pack sync.",
    )
    r2_access_key_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRAILFORGE_R2_ACCESS_KEY_ID",
            "R2_ACCESS_KEY_ID",
            "r2_access_key_id",
        ),
        description="Cloudflare R2 S3 API access key ID.",
    )
    r2_secret_access_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRAILFORGE_R2_SECRET_ACCESS_KEY",
            "R2_SECRET_ACCESS_KEY",
            "r2_secret_access_key",
        ),
        description="Cloudflare R2 S3 API secret access key.",
    )

    # AI & Model Context Protocol (MCP) + Self-Hosted OAuth 2.1 (v1.2, SPEC_AI_MCP Sec 9.1)
    enable_mcp: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRAILFORGE_ENABLE_MCP",
            "ENABLE_MCP",
            "enable_mcp",
        ),
        description="Master toggle for /mcp, /mcp/auth, and /.well-known/oauth-* endpoints.",
    )
    mcp_auth_mode: Literal["hybrid", "oauth_required", "anonymous_only"] = Field(
        default="hybrid",
        validation_alias=AliasChoices(
            "TRAILFORGE_MCP_AUTH_MODE",
            "MCP_AUTH_MODE",
            "mcp_auth_mode",
        ),
        description="MCP auth policy ('hybrid', 'oauth_required', or 'anonymous_only').",
    )
    public_base_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRAILFORGE_PUBLIC_BASE_URL",
            "PUBLIC_BASE_URL",
            "public_base_url",
        ),
        description=(
            "Canonical public HTTPS origin (e.g. https://trails.example.com) for deep links "
            "and OAuth 2.1 discovery metadata."
        ),
    )
    rate_limit_mcp_per_min: int = Field(
        default=60,
        ge=1,
        validation_alias=AliasChoices(
            "TRAILFORGE_RATE_LIMIT_MCP_PER_MIN",
            "RATE_LIMIT_MCP_PER_MIN",
            "rate_limit_mcp_per_min",
        ),
        description="Per-IP sliding-window request cap per minute on /mcp and /mcp/*.",
    )
    mcp_app_html: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRAILFORGE_MCP_APP_HTML",
            "MCP_APP_HTML",
            "mcp_app_html",
        ),
        description="Optional local file path override for ui://trailforge/explorer.html.",
    )
    mcp_csp_tile_domains: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRAILFORGE_MCP_CSP_TILE_DOMAINS",
            "MCP_CSP_TILE_DOMAINS",
            "mcp_csp_tile_domains",
        ),
        description=(
            "Comma-separated bare origins allowed in MCP App _meta.ui.csp "
            "(auto-includes TILES_PUBLIC_BASE_URL origin when configured)."
        ),
    )
    oauth_access_token_ttl_s: int = Field(
        default=3600,
        ge=60,
        le=86400,
        validation_alias=AliasChoices(
            "TRAILFORGE_OAUTH_ACCESS_TOKEN_TTL_S",
            "OAUTH_ACCESS_TOKEN_TTL_S",
            "oauth_access_token_ttl_s",
        ),
        description="OAuth 2.1 access token lifetime in seconds.",
    )
    oauth_refresh_token_ttl_s: int = Field(
        default=2_592_000,
        ge=300,
        le=31_536_000,
        validation_alias=AliasChoices(
            "TRAILFORGE_OAUTH_REFRESH_TOKEN_TTL_S",
            "OAUTH_REFRESH_TOKEN_TTL_S",
            "oauth_refresh_token_ttl_s",
        ),
        description="OAuth 2.1 refresh token lifetime in seconds (default 30 days).",
    )
    oauth_dcr_max_per_ip_per_day: int = Field(
        default=20,
        ge=1,
        le=10_000,
        validation_alias=AliasChoices(
            "TRAILFORGE_OAUTH_DCR_MAX_PER_IP_PER_DAY",
            "OAUTH_DCR_MAX_PER_IP_PER_DAY",
            "oauth_dcr_max_per_ip_per_day",
        ),
        description="Daily per-IP cap on RFC 7591 OAuth Dynamic Client Registration.",
    )

    @property
    def uploads_max_total_bytes(self) -> int:
        """Global photo uploads quota expressed in bytes."""
        return self.uploads_max_total_mb * 1024 * 1024

    @property
    def trailforge_tiles_dir(self) -> str:
        """Backward-compatible alias for `tiles_dir`."""
        return self.tiles_dir

    @property
    def trailforge_packs_dir(self) -> str:
        """Backward-compatible alias for `packs_dir`."""
        return self.packs_dir

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def enabled_states_list(self) -> list[str]:
        return [state.strip().upper() for state in self.enabled_states.split(",") if state.strip()]

    @property
    def should_trust_cloudflare_headers(self) -> bool:
        """True when CF-Connecting-IP may be honored (origin secret set or explicit flag)."""
        return bool(self.trust_cloudflare_headers or self.origin_shared_secret.strip())

    @property
    def is_production_mode(self) -> bool:
        """True when running in hardened/production mode (HTTPS cookies, Railway, or locked reg)."""
        railway_env = (os.environ.get("RAILWAY_ENVIRONMENT") or "").strip()
        return bool(
            self.auth_cookie_secure
            or railway_env
            or self.registration_mode != "open"
        )

    @property
    def docs_enabled(self) -> bool:
        """Whether OpenAPI /docs, /redoc, and /openapi.json should be exposed."""
        if self.enable_docs is not None:
            return bool(self.enable_docs)
        return not self.is_production_mode


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
