"""asset title for creative library

Revision ID: 3f2a1c9d5e47
Revises: 17d993e08c71
"""

import sqlalchemy as sa
from alembic import op

revision = "3f2a1c9d5e47"
down_revision = "17d993e08c71"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("title", sa.String(length=80)))


def downgrade() -> None:
    op.drop_column("assets", "title")
