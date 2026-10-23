"""asset library_hidden for hidden-style deletion

Revision ID: a7c4e2f90b13
Revises: 3f2a1c9d5e47
"""

import sqlalchemy as sa
from alembic import op

revision = "a7c4e2f90b13"
down_revision = "3f2a1c9d5e47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assets",
        sa.Column("library_hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("assets", "library_hidden")
