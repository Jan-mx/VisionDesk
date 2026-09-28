"""run heartbeat and stale-run cleanup

Revision ID: 17d993e08c71
Revises: fa850313b768
"""

import sqlalchemy as sa
from alembic import op

revision = "17d993e08c71"
down_revision = "fa850313b768"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tool_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True)))
    op.execute(
        """
        UPDATE tool_runs
        SET status = 'failed',
            stage = '已结束',
            error = '任务执行超时或工作进程已中断，请重试',
            finished_at = now(),
            heartbeat_at = now()
        WHERE status IN ('queued', 'running')
          AND created_at < now() - interval '5 minutes'
        """
    )


def downgrade() -> None:
    op.drop_column("tool_runs", "heartbeat_at")
