"""Bind OAuth authorization codes to the session version that approved them.

Revision ID: 0012_oauth_code_session_version
Revises: 0011_closure_datasets
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_oauth_code_session_version"
down_revision = "0011_closure_datasets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # For existing codes the approving session version is unknown. Keep the
    # consumed rows so replay can still revoke their original token families.
    op.add_column(
        "oauth_authorization_codes",
        sa.Column("user_token_version", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("oauth_authorization_codes", "user_token_version")
