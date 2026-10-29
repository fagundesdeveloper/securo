import uuid
from datetime import date as _Date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.workspace_context import (
    WorkspaceContext,
    current_workspace,
    current_writable_workspace,
)
from app.schemas.action_plan import ActionPlanCreate, ActionPlanRead, ActionPlanUpdate
from app.services import action_plan_service
from app.services.action_plan_service import DuplicatePlanError

router = APIRouter(prefix="/api/action-plans", tags=["action-plans"])


@router.get("", response_model=list[ActionPlanRead])
async def list_action_plans(
    month: Optional[_Date] = Query(None, description="Return only the plan for this month"),
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    if month is not None:
        plan = await action_plan_service.get_plan_for_month(session, ctx.workspace.id, month)
        return [plan] if plan else []
    return await action_plan_service.list_plans(session, ctx.workspace.id)


@router.post("", response_model=ActionPlanRead, status_code=status.HTTP_201_CREATED)
async def create_action_plan(
    data: ActionPlanCreate,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    try:
        return await action_plan_service.create_plan(session, ctx.workspace.id, ctx.user_id, data)
    except DuplicatePlanError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.patch("/{plan_id}", response_model=ActionPlanRead)
async def update_action_plan(
    plan_id: uuid.UUID,
    data: ActionPlanUpdate,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    plan = await action_plan_service.update_plan(session, plan_id, ctx.workspace.id, data)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    return plan


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_action_plan(
    plan_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    if not await action_plan_service.delete_plan(session, plan_id, ctx.workspace.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
