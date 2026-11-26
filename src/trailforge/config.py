"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
        default="trailforge-dev-secret-change-in-production",
        validation_alias=AliasChoices("AUTH_SECRET_KEY", "TRAILFORGE_AUTH_SECRET_KEY"),
    )
    privacy_secret_key: str = Field(
        default="trailforge-dev-privacy-secret-change-in-production",
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
        validation_alias=AliasChoices("TILES_DIR", "TRAILFORGE_TILES_DIR"),
        description="Directory storing generated vector/terrain PMTiles and manifest.json.",
    )
    uploads_dir: str = Field(
        default="/data/uploads",
        validation_alias=AliasChoices("TRAILFORGE_UPLOADS_DIR", "UPLOADS_DIR"),
        description="Directory storing validated community condition report photo attachments.",
    )
    packs_dir: str = Field(
        default="/data/packs",
        validation_alias=AliasChoices("PACKS_DIR", "TRAILFORGE_PACKS_DIR"),
        description="Directory storing built offline regional pack bundles and manifests.",
    )

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
