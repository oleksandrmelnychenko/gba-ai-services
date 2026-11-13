"""Durable seller contact contract, independent from generated tasks."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

ContactResult = Literal["reached", "no_answer", "proposal", "declined", "promised_payment"]
ContactBucket = Literal["all", "overdue", "today", "upcoming", "unscheduled"]


class ContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    client_id: int = Field(gt=0, strict=True)
    result: ContactResult
    note: str | None = Field(default=None, max_length=4000)
    next_contact_at: AwareDatetime | None = Field(...)
    next_action: str | None = Field(..., max_length=500)

    @field_validator("event_id")
    @classmethod
    def nonempty_event_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("event_id must not be empty")
        return value

    @field_validator("next_contact_at", mode="before")
    @classmethod
    def explicit_datetime_offset(cls, value: object) -> object:
        if value is None or isinstance(value, datetime):
            return value
        if not isinstance(value, str) or not re.search(r"T.*(?:Z|[+-]\d{2}:\d{2})$", value):
            raise ValueError("next_contact_at must be an ISO timestamp with explicit offset")
        return value

    @model_validator(mode="after")
    def validate_agreement(self) -> ContactCreate:
        if (self.next_contact_at is None) != (self.next_action is None):
            raise ValueError("next_contact_at and next_action must be supplied together")
        if self.next_action is not None and not self.next_action.strip():
            raise ValueError("next_action must not be blank")
        if self.next_contact_at is not None:
            value = self.next_contact_at.astimezone(UTC)
            self.next_contact_at = value.replace(microsecond=(value.microsecond // 1000) * 1000)
        return self


class ContactEvent(BaseModel):
    event_id: str
    client_id: int
    client_name: str
    client_net_uid: UUID | None
    manager_id: int
    manager_name: str
    result: ContactResult
    note: str | None
    next_contact_at: datetime | None
    next_action: str | None
    at: datetime
