import uuid
from datetime import date as _Date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator


class ActionPlanBase(BaseModel):
    month: _Date
    body: str

    @field_validator("month")
    @classmethod
    def normalize_to_first_of_month(cls, value: _Date) -> _Date:
        """A plan is for a month, not a day.

        Snapping here rather than trusting the caller keeps the unique
        constraint meaningful: without it, two plans for November written
        from different days of the month would both be stored.
        """
        return value.replace(day=1)

    @field_validator("body")
    @classmethod
    def body_must_say_something(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A plan cannot be empty")
        return value.strip()


class ActionPlanCreate(ActionPlanBase):
    pass


class ActionPlanUpdate(BaseModel):
    body: Optional[str] = None
    # The verdict. Writing it is what marks the plan reviewed; sending an
    # empty string clears it and makes the plan open again.
    review: Optional[str] = None

    @field_validator("body")
    @classmethod
    def body_must_say_something(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("A plan cannot be empty")
        return value.strip() if value is not None else None


class ActionPlanRead(ActionPlanBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    review: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
