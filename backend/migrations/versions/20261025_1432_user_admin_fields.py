"""user role/status/last_login_at/token_version for admin foundation

Revision ID: d4a8f0c3b752
Revises: a7c4e2f90b13
"""

import sqlalchemy as sa
from alembic import op

revision = "d4a8f0c3b752"
down_revision = "a7c4e2f90b13"
branch_labels = None
depends_on = None

# 与 app/models/base.py enum_column 一致：VARCHAR 存枚举值，无原生枚举。
# 迁移自包含，不反向依赖 app.models，模型后续演进不影响本迁移重放。
_ENUM_LENGTH = 16


def _varchar_enum(*values: str) -> sa.Enum:
    return sa.Enum(*values, native_enum=False, create_constraint=False, length=_ENUM_LENGTH)


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("role", _varchar_enum("user", "admin"), nullable=False, server_default="user"),
    )
    op.add_column(
        "users",
        sa.Column(
            "status",
            _varchar_enum("active", "disabled", "deleting"),
            nullable=False,
            server_default="active",
        ),
    )
    # 可空不回填：初次成功登录前为空，不从注册时间推算（PRD §6.2）
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="1"),
    )
    # 全库最多一位管理员（PRD §7）；建索引前无任何 admin 行，旧数据不受影响
    op.create_index(
        "uq_users_single_admin",
        "users",
        ["role"],
        unique=True,
        postgresql_where=sa.text("role = 'admin'"),
    )


def downgrade() -> None:
    op.drop_index("uq_users_single_admin", table_name="users")
    op.drop_column("users", "token_version")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "status")
    op.drop_column("users", "role")
