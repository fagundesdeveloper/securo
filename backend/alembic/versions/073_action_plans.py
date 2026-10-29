"""action plans: what the month was supposed to be, and what it turned out to be

Revision ID: 073
Revises: 072
Create Date: 2026-10-29

One additive table, no backfill. A plan is prose the user (or the agent on
their behalf) writes before a month starts, and `review` is the verdict
written after it closes — the presence of that verdict is what marks a plan
reviewed, so there is no status column to fall out of step with it.

Unique on (workspace_id, month) because a month with two plans is two
answers to the same question and the review would have to pick one. Scoped
to the workspace rather than the user: a household plan belongs to the
household, and both partners see the same one.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "073"
down_revision = "072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "action_plans",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("review", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "month", name="uq_action_plan_per_workspace_month"),
    )
    op.create_index("ix_action_plans_workspace_id", "action_plans", ["workspace_id"])
    op.create_index("ix_action_plans_month", "action_plans", ["month"])


def downgrade() -> None:
    op.drop_index("ix_action_plans_month", table_name="action_plans")
    op.drop_index("ix_action_plans_workspace_id", table_name="action_plans")
    op.drop_table("action_plans")
