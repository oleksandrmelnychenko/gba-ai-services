"""Domain models for the NBA sales-task engine (persisted to MongoDB)."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class TaskType(StrEnum):
    REORDER_DUE = "reorder_due"
    DEBT_FOLLOWUP = "debt_followup"
    CROSS_SELL = "cross_sell"
    CHURN_WINBACK = "churn_winback"
    NEW_CLIENT_ACTIVATION = "new_client_activation"


class TaskStatus(StrEnum):
    GENERATED = "generated"
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SNOOZED = "snoozed"
    DISMISSED = "dismissed"


class Urgency(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


# Allowed status transitions (the lifecycle state machine — enforced in services/lifecycle.py).
ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.GENERATED: {TaskStatus.OPEN},
    TaskStatus.OPEN: {TaskStatus.IN_PROGRESS, TaskStatus.DONE, TaskStatus.SNOOZED, TaskStatus.DISMISSED},
    TaskStatus.IN_PROGRESS: {TaskStatus.DONE, TaskStatus.SNOOZED, TaskStatus.DISMISSED, TaskStatus.OPEN},
    TaskStatus.SNOOZED: {TaskStatus.OPEN, TaskStatus.DISMISSED},
    TaskStatus.DONE: set(),         # terminal
    TaskStatus.DISMISSED: set(),    # terminal
}

TERMINAL = {TaskStatus.DONE, TaskStatus.DISMISSED}
ACTIVE = {TaskStatus.GENERATED, TaskStatus.OPEN, TaskStatus.IN_PROGRESS, TaskStatus.SNOOZED}


class Note(BaseModel):
    author_id: int
    text: str
    created_at: datetime


class StatusChange(BaseModel):
    from_status: str = Field(alias="from")
    to_status: str = Field(alias="to")
    at: datetime
    by: int | str               # manager_id or "system"
    reason: str | None = None
    outcome: Outcome | None = None

    model_config = {"populate_by_name": True}


class Outcome(BaseModel):
    sold: bool = False
    amount: float | None = None
    note: str | None = None


class Contact(BaseModel):
    phone: str | None = None
    email: str | None = None
    viber: str | None = None
    preferred: str | None = None


class Explanation(BaseModel):
    factors: list[str] = Field(default_factory=list)
    source_signal: str = ""
    confidence: float = 0.0


class Task(BaseModel):
    task_key: str                 # unique dedup key: mgr|client|type|window
    manager_id: int
    client_id: int
    client_name: str | None = None
    task_type: TaskType
    title: str
    reason: str
    priority: float = 0.0          # 0..100 = 100 * p_outcome (the unchanged contract / sort field)
    p_outcome: float = 0.0         # calibrated P(outcome in (T,T+H] | task) from the propensity model
    expected_value: float = 0.0    # documented E[value] in EUR (cash/turnover at stake)
    ev_score: float = 0.0          # p_outcome * expected_value — expected-EUR ordering for the cockpit
    urgency: Urgency = Urgency.NORMAL
    status: TaskStatus = TaskStatus.GENERATED
    payload: dict = Field(default_factory=dict)
    signals: dict = Field(default_factory=dict)
    explanation: Explanation = Field(default_factory=Explanation)
    contact: Contact = Field(default_factory=Contact)
    due_date: datetime | None = None
    sla_breached: bool = False
    escalated_to: int | None = None
    notes: list[Note] = Field(default_factory=list)
    status_history: list[StatusChange] = Field(default_factory=list)
    snooze_until: datetime | None = None
    outcome: Outcome | None = None
    ab_variant: str | None = None
    model_version: str = "nba-v1"
    in_progress_since: datetime | None = None  # UTC stamp set when first moved to IN_PROGRESS (work-started)
    generated_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None
