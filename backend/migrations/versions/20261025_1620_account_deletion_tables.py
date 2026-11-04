"""account deletion operations + pending object writes (WI-20261025-03 决策 1)

Revision ID: b7e2d9c4a815
Revises: c5f8a3e71b60
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b7e2d9c4a815"
down_revision = "c5f8a3e71b60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 账号清理操作：target/actor 不建外键（PRD §7），用户墓碑删除后仍可查询。
    # 部分唯一索引保证同一目标最多一个 processing/failed 操作（WI 决策 1）。
    op.create_table(
        "account_deletion_operations",
        sa.Column("target_user_id", sa.UUID(), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("scope_digest", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("progress_done", sa.Integer(), server_default="0", nullable=False),
        sa.Column("progress_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.String(length=500), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_account_deletion_active",
        "account_deletion_operations",
        ["target_user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('processing', 'failed')"),
    )

    # 在途对象写入登记（PRD §6.4 写入冻结屏障）：不建外键，登记先于资产行存在。
    op.create_table(
        "pending_object_writes",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("retries", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pending_object_writes_user", "pending_object_writes", ["user_id"], unique=False
    )
    op.create_index(
        "ix_pending_object_writes_state_created",
        "pending_object_writes",
        ["state", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pending_object_writes_state_created", table_name="pending_object_writes"
    )
    op.drop_index("ix_pending_object_writes_user", table_name="pending_object_writes")
    op.drop_table("pending_object_writes")
    op.drop_index(
        "uq_account_deletion_active", table_name="account_deletion_operations"
    )
    op.drop_table("account_deletion_operations")
