"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
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

    # Local storage directories (streaming pack/tile outputs)
    trailforge_data_dir: str = Field(default="/data")
    trailforge_tiles_dir: str = Field(default="/data/tiles")
    trailforge_packs_dir: str = Field(default="/data/packs")

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
