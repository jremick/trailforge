"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache

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

    # Auth / self-hosted session settings (zero external IdP)
    auth_secret_key: str = Field(
        default="trailforge-dev-secret-change-in-production",
        validation_alias=AliasChoices("AUTH_SECRET_KEY", "TRAILFORGE_AUTH_SECRET_KEY"),
    )
    auth_token_ttl_seconds: int = Field(default=60 * 60 * 24 * 14, ge=300)
    auth_cookie_name: str = Field(default="tf_session")
    auth_cookie_secure: bool = Field(default=False)

    # Local storage directories (streaming pack/tile outputs)
    trailforge_data_dir: str = Field(default="/data")
    tiles_dir: str = Field(
        default="/data/tiles",
        validation_alias=AliasChoices("TILES_DIR", "TRAILFORGE_TILES_DIR"),
        description="Directory storing generated vector/terrain PMTiles and manifest.json.",
    )
    trailforge_packs_dir: str = Field(default="/data/packs")

    @property
    def trailforge_tiles_dir(self) -> str:
        """Backward-compatible alias for `tiles_dir`."""
        return self.tiles_dir

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
