import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Date, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.user import User


class ActionPlan(Base):
    """What the household intends to do with its money in one month.

    Written before the month starts and judged after it ends, which is the
    whole point: a plan nobody looks back at is a wish. `review` holds that
    verdict, and its presence is what makes a plan reviewed — no status
    column to drift out of step with it.

    One plan per month per workspace. A month with two plans is two answers
    to the same question, and the review would have to pick one.
    """

    __tablename__ = "action_plans"
    __table_args__ = (
        UniqueConstraint("workspace_id", "month", name="uq_action_plan_per_workspace_month"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    # First day of the month the plan is for, matching `budgets.month` so the
    # two line up without a conversion.
    month: Mapped[date] = mapped_column(Date, index=True)
    # The plan itself, as the user or the agent wrote it. Free text on
    # purpose: a plan is prose with numbers in it, and forcing it into rows
    # would make the agent's version lossy and the user's version a chore.
    body: Mapped[str] = mapped_column(Text)
    # The verdict, written after the month closed. Null until then.
    review: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship()
