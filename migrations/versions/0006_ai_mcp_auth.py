"""Add Tier C AI/MCP authentication tables (PATs & OAuth 2.1 + PKCE + DCR).

Revision ID: 0006_ai_mcp_auth
Revises: 0005_user_token_version
Create Date: 2026-11-27 08:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006_ai_mcp_auth"
down_revision: str | Sequence[str] | None = "0005_user_token_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # All four tables belong strictly to Tier C (zero OSM geometries, zero OSM tag blobs).

    # 1. Personal Access Tokens (`tf_pat_...`, SHA-256 hash only)
    op.create_table(
        "personal_access_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_token_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("ARRAY['trailforge:read']::text[]"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_personal_access_tokens_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_personal_access_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_personal_access_tokens_token_hash"),
    )
    op.create_index(
        "ix_personal_access_tokens_user_id",
        "personal_access_tokens",
        ["user_id"],
        unique=False,
    )

    # 2. OAuth 2.1 Dynamic Client Registrations (RFC 7591)
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("client_name", sa.String(length=120), nullable=False),
        sa.Column("client_secret_hash", sa.String(length=64), nullable=True),
        sa.Column("redirect_uris", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("grant_types", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("token_endpoint_auth_method", sa.String(length=32), nullable=False),
        sa.Column("registered_by_ip_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "token_endpoint_auth_method IN ('none','client_secret_post')",
            name="auth_method_valid",
        ),
        sa.PrimaryKeyConstraint("client_id", name="pk_oauth_clients"),
    )
    op.create_index("ix_oauth_clients_created_at", "oauth_clients", ["created_at"], unique=False)
    op.create_index(
        "ix_oauth_clients_ip_created",
        "oauth_clients",
        ["registered_by_ip_hash", "created_at"],
        unique=False,
    )

    # 3. Single-use OAuth 2.1 Authorization Codes (60 s TTL, PKCE S256 mandatory)
    op.create_table(
        "oauth_authorization_codes",
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.String(length=128), nullable=False),
        sa.Column("code_challenge_method", sa.String(length=16), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("code_challenge_method = 'S256'", name="pkce_s256"),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["oauth_clients.client_id"],
            name="fk_oauth_authorization_codes_client_id_oauth_clients",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_oauth_authorization_codes_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("code_hash", name="pk_oauth_authorization_codes"),
    )
    op.create_index(
        "ix_oauth_authorization_codes_client_id",
        "oauth_authorization_codes",
        ["client_id"],
        unique=False,
    )
    op.create_index(
        "ix_oauth_authorization_codes_expires_at",
        "oauth_authorization_codes",
        ["expires_at"],
        unique=False,
    )

    # 4. OAuth 2.1 Access & Rotating Refresh Tokens (with family-wide reuse revocation)
    op.create_table(
        "oauth_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("token_family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_token_version", sa.Integer(), nullable=False),
        sa.Column("access_token_hash", sa.String(length=64), nullable=False),
        sa.Column("refresh_token_hash", sa.String(length=64), nullable=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["oauth_clients.client_id"],
            name="fk_oauth_tokens_client_id_oauth_clients",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_oauth_tokens_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_oauth_tokens"),
        sa.UniqueConstraint("access_token_hash", name="uq_oauth_tokens_access_token_hash"),
        sa.UniqueConstraint("refresh_token_hash", name="uq_oauth_tokens_refresh_token_hash"),
    )
    op.create_index(
        "ix_oauth_tokens_token_family_id",
        "oauth_tokens",
        ["token_family_id"],
        unique=False,
    )
    op.create_index(
        "ix_oauth_tokens_user_id",
        "oauth_tokens",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_oauth_tokens_client_user",
        "oauth_tokens",
        ["client_id", "user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_oauth_tokens_client_user", table_name="oauth_tokens")
    op.drop_index("ix_oauth_tokens_user_id", table_name="oauth_tokens")
    op.drop_index("ix_oauth_tokens_token_family_id", table_name="oauth_tokens")
    op.drop_table("oauth_tokens")

    op.drop_index("ix_oauth_authorization_codes_expires_at", table_name="oauth_authorization_codes")
    op.drop_index("ix_oauth_authorization_codes_client_id", table_name="oauth_authorization_codes")
    op.drop_table("oauth_authorization_codes")

    op.drop_index("ix_oauth_clients_ip_created", table_name="oauth_clients")
    op.drop_index("ix_oauth_clients_created_at", table_name="oauth_clients")
    op.drop_table("oauth_clients")

    op.drop_index("ix_personal_access_tokens_user_id", table_name="personal_access_tokens")
    op.drop_table("personal_access_tokens")
