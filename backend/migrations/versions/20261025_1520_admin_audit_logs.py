"""admin_audit_logs for management action audit

Revision ID: c5f8a3e71b60
Revises: d4a8f0c3b752
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c5f8a3e71b60"
down_revision = "d4a8f0c3b752"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 审计行一律不建外键（PRD §7）：不随目标业务行删除而级联消失，
    # actor/target 仅存 UUID 值。summary 为白名单键值摘要（脱敏在服务层完成）。
    op.create_table(
        "admin_audit_logs",
        sa.Column("actor_id", sa.UUID(), nullable=False),
        sa.Column("target_type", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("operation_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_audit_logs_target", "admin_audit_logs", ["target_type", "target_id"], unique=False
    )
    op.create_index("ix_admin_audit_logs_actor", "admin_audit_logs", ["actor_id"], unique=False)
    op.create_index(
        "ix_admin_audit_logs_created_at", "admin_audit_logs", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_admin_audit_logs_created_at", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_actor", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_target", table_name="admin_audit_logs")
    op.drop_table("admin_audit_logs")
