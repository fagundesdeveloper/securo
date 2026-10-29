"""Action plans: one per month per workspace, written ahead and judged after."""
import uuid
from datetime import date as _Date, datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.action_plan import ActionPlan
from app.schemas.action_plan import ActionPlanCreate, ActionPlanUpdate


class DuplicatePlanError(Exception):
    """A plan already exists for that month in this workspace."""


async def list_plans(
    session: AsyncSession, workspace_id: uuid.UUID, limit: int = 24
) -> list[ActionPlan]:
    """Most recent month first — the one being planned or reviewed is on top."""
    result = await session.execute(
        select(ActionPlan)
        .where(ActionPlan.workspace_id == workspace_id)
        .order_by(ActionPlan.month.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def get_plan(
    session: AsyncSession, plan_id: uuid.UUID, workspace_id: uuid.UUID
) -> Optional[ActionPlan]:
    result = await session.execute(
        select(ActionPlan).where(
            ActionPlan.id == plan_id, ActionPlan.workspace_id == workspace_id
        )
    )
    return result.scalar_one_or_none()


async def get_plan_for_month(
    session: AsyncSession, workspace_id: uuid.UUID, month: _Date
) -> Optional[ActionPlan]:
    result = await session.execute(
        select(ActionPlan).where(
            ActionPlan.workspace_id == workspace_id,
            ActionPlan.month == month.replace(day=1),
        )
    )
    return result.scalar_one_or_none()


async def create_plan(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    data: ActionPlanCreate,
) -> ActionPlan:
    # Checked here as well as by the unique constraint so the API can answer
    # 409 with a useful message instead of leaking an IntegrityError.
    if await get_plan_for_month(session, workspace_id, data.month) is not None:
        raise DuplicatePlanError(f"A plan for {data.month:%Y-%m} already exists")
    plan = ActionPlan(
        workspace_id=workspace_id, user_id=user_id, month=data.month, body=data.body
    )
    session.add(plan)
    await session.commit()
    await session.refresh(plan)
    return plan


async def update_plan(
    session: AsyncSession,
    plan_id: uuid.UUID,
    workspace_id: uuid.UUID,
    data: ActionPlanUpdate,
) -> Optional[ActionPlan]:
    plan = await get_plan(session, plan_id, workspace_id)
    if plan is None:
        return None
    fields = data.model_dump(exclude_unset=True)
    if "body" in fields and fields["body"] is not None:
        plan.body = fields["body"]
    if "review" in fields:
        review = (fields["review"] or "").strip()
        # An empty review reopens the plan: the timestamp has to go with it,
        # or the row claims a verdict it no longer carries.
        plan.review = review or None
        plan.reviewed_at = datetime.now(timezone.utc) if review else None
    await session.commit()
    await session.refresh(plan)
    return plan


async def delete_plan(
    session: AsyncSession, plan_id: uuid.UUID, workspace_id: uuid.UUID
) -> bool:
    plan = await get_plan(session, plan_id, workspace_id)
    if plan is None:
        return False
    await session.delete(plan)
    await session.commit()
    return True
