from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import action_plan_service
from mcp_server.auth import CallContext
from mcp_server.registry import tool
from mcp_server.tools._helpers import resolve_workspace_id


@tool(
    name="list_action_plans",
    description=(
        "Return the household's monthly action plans, newest month first: what "
        "was intended for each month and, when the month has been judged, the "
        "verdict that was recorded. Use this before reviewing a month so the "
        "judgement is against what was actually promised, and before proposing "
        "a new plan so it does not repeat an action that already failed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "How many months back to return (default 12)",
                "minimum": 1,
                "maximum": 24,
            },
        },
        "additionalProperties": False,
    },
    tags=["read", "action_plans"],
)
async def list_action_plans(
    *,
    session: AsyncSession,
    ctx: CallContext,
    limit: int | None = None,
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    plans = await action_plan_service.list_plans(session, ws_id, limit=limit or 12)
    return {
        "items": [
            {
                "id": str(p.id),
                "month": p.month.isoformat(),
                "body": p.body,
                "review": p.review,
                # Whether the month has been judged. Derived from `review`
                # rather than stored, so it cannot disagree with it.
                "reviewed": p.review is not None,
                "reviewed_at": p.reviewed_at.isoformat() if p.reviewed_at else None,
            }
            for p in plans
        ],
        "total": len(plans),
    }
