"""Pydantic v2 data models for ratchet tasks and plan steps.

No I/O, no side effects. Pure data definitions.
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Task models
# ---------------------------------------------------------------------------


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

    Flat model -- does not inherit TaskDescription -- for clean
    model_dump / model_validate round-trips and unambiguous field defaults.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
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


# ---------------------------------------------------------------------------
# Step models
# ---------------------------------------------------------------------------


class StepType(StrEnum):
    """Step classification used to select executor sandbox and model."""

    DISCOVERY_STEP = "discovery_step"
    IMPLEMENT_STEP = "implement_step"
    SIMPLE_TASK_STEP = "simple_task_step"
    VERIFY_STEP = "verify_step"
    UPDATE_DOCS_STEP = "update_docs_step"


class StepIntent(StrEnum):
    """Semantic intent of a step, used for reporting and routing."""

    NEW_FEATURE = "new_feature"
    MODIFY = "modify"
    BUGFIX = "bugfix"
    REFACTOR = "refactor"
    TEST = "test"
    CONFIG = "config"
    DOCS = "docs"


class ValidatorSpec(BaseModel):
    """Validation configuration attached to each step."""

    level: Annotated[int, Field(ge=1, le=5)]
    success_criterion: str
    command: str | None = None
    extra_context: str | None = None


class Step(BaseModel):
    """Core unit of execution within a plan.

    Each step is created by the planner via an add_*_step tool and stored
    in the PlanStore. The step briefing becomes the executor's system prompt
    append; the validator spec controls post-execution validation.
    """

    id: str
    type: StepType
    goal: str
    briefing: str
    target_files: list[str] = Field(default_factory=list)
    creates_files: list[str] = Field(default_factory=list)
    deletes_files: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    validator: ValidatorSpec
    intent: StepIntent | None = None


class StepOutput(BaseModel):
    """Structured output returned by the executor via output_format."""

    summary: str
    artifacts: dict[str, object] = Field(default_factory=dict)
    notes: str = ""


class StepResult(BaseModel):
    """Internal struct used by the executor module before validation."""

    step_id: str
    output: StepOutput | None
    success: bool
    error: str | None = None


class ValidationVerdict(BaseModel):
    """Verdict returned by the validator after step execution.

    Levels 2-5 produce it via output_format; level 1 constructs it
    directly from subprocess.run exit code.
    """

    passed: bool
    diagnosis: str = ""
    suggested_fixes: list[str] = Field(default_factory=list)
