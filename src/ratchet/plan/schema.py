"""Pydantic v2 data models for ratchet tasks and plan steps.

No I/O, no side effects. Pure data definitions.
"""

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    """Lifecycle states for a Task."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskCategory(StrEnum):
    """Classification hint for a Task, used by downstream routing."""

    BUG_FIX = "bug_fix"
    FEATURE = "feature"
    REFACTOR = "refactor"
    INVESTIGATION = "investigation"
    TEST = "test"
    DOCUMENTATION = "documentation"
    OTHER = "other"


class TaskDescription(BaseModel):
    """Planner-supplied fields that describe a task.

    Contains only the fields the planner provides at creation time.
    System-generated fields (id, status, timestamps) live on Task.
    """

    title: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(min_length=1)]
    category: TaskCategory
    repo_path: Annotated[str, Field(min_length=1)]
    issue_id: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    notes: str | None = None


class Task(BaseModel):
    """A unit of work defined by the planner.

    Flat model — does not inherit TaskDescription — for clean
    model_dump / model_validate round-trips and unambiguous field defaults.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    title: str
    description: str
    category: TaskCategory
    repo_path: str
    issue_id: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    notes: str | None = None

    @classmethod
    def from_description(cls, desc: TaskDescription) -> "Task":
        """Construct a new Task from a TaskDescription.

        Args:
            desc: Planner-supplied description fields.

        Returns:
            A new Task with pending status and generated id/timestamps.
        """
        return cls(**desc.model_dump())
