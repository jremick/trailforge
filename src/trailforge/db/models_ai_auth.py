"""SQLAlchemy 2 Tier C ORM models for Personal Access Tokens and OAuth 2.1 + PKCE + DCR.

All four tables live strictly in Tier C (zero OSM geometries, zero OSM tag blobs) and
store only SHA-256 digests of secrets, tokens, and authorization codes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trailforge.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


_JSONB_TYPE = JSON().with_variant(JSONB(), "postgresql")
_TEXT_ARRAY_TYPE = JSON().with_variant(ARRAY(Text), "postgresql")


class PersonalAccessToken(Base):
    """Scoped self-hosted Personal Access Token (`tf_pat_...`, stored as SHA-256 digest only)."""

    __tablename__ = "personal_access_tokens"
    __table_args__ = (Index("ix_personal_access_tokens_user_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    user_token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scopes: Mapped[list[str]] = mapped_column(
        _TEXT_ARRAY_TYPE,
        nullable=False,
        default=lambda: ["trailforge:read"],
        server_default=text("ARRAY['trailforge:read']::text[]"),
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class OAuthClient(Base):
    """Dynamically registered OAuth 2.1 client (RFC 7591)."""

    __tablename__ = "oauth_clients"
    __table_args__ = (
        CheckConstraint(
            "token_endpoint_auth_method IN ('none','client_secret_post')",
            name="auth_method_valid",
        ),
        Index("ix_oauth_clients_created_at", "created_at"),
        Index("ix_oauth_clients_ip_created", "registered_by_ip_hash", "created_at"),
    )

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_name: Mapped[str] = mapped_column(String(120), nullable=False)
    client_secret_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    redirect_uris: Mapped[list[str]] = mapped_column(_JSONB_TYPE, nullable=False)
    grant_types: Mapped[list[str]] = mapped_column(_JSONB_TYPE, nullable=False)
    token_endpoint_auth_method: Mapped[str] = mapped_column(String(32), nullable=False)
    registered_by_ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OAuthAuthorizationCode(Base):
    """Single-use OAuth 2.1 authorization code (60 s TTL, PKCE S256 mandatory)."""

    __tablename__ = "oauth_authorization_codes"
    __table_args__ = (
        CheckConstraint("code_challenge_method = 'S256'", name="pkce_s256"),
        Index("ix_oauth_authorization_codes_client_id", "client_id"),
        Index("ix_oauth_authorization_codes_expires_at", "expires_at"),
    )

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    user_token_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    code_challenge_method: Mapped[str] = mapped_column(String(16), nullable=False)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_family_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class OAuthToken(Base):
    """Issued OAuth 2.1 access and rotating refresh token pair grouped by token_family_id."""

    __tablename__ = "oauth_tokens"
    __table_args__ = (
        Index("ix_oauth_tokens_token_family_id", "token_family_id"),
        Index("ix_oauth_tokens_user_id", "user_id"),
        Index("ix_oauth_tokens_client_user", "client_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    token_family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    client_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    user_token_version: Mapped[int] = mapped_column(Integer, nullable=False)
    access_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    refresh_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


__all__ = [
    "OAuthAuthorizationCode",
    "OAuthClient",
    "OAuthToken",
    "PersonalAccessToken",
]
