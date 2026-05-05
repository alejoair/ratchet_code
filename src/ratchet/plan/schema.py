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

    Each step is created by the planner via the add_step tool and stored
    in the PlanStore. The step briefing becomes the executor's system prompt
    append; the validator spec controls post-execution validation.

    File tracking fields (target_files, creates_files, deletes_files) describe
    the step's intended file system modifications. Implementation detail fields
    (function_signatures, imports, classes, code_snippets) provide optional
    context about code structures and patterns relevant to the step.
    """

    id: str
    type: StepType
    goal: str
    briefing: str
    target_files: list[str] = Field(default_factory=list)
    creates_files: list[str] = Field(default_factory=list)
    deletes_files: list[str] = Field(default_factory=list)
    function_signatures: list[str] = Field(
        default_factory=list,
        description=(
            "Function signatures to add or modify "
            "(e.g. 'def foo(x: int) -> str:')"
        ),
    )
    imports: list[str] = Field(
        default_factory=list,
        description="New imports to add (e.g., 'from typing import Optional')"
    )
    classes: list[str] = Field(
        default_factory=list,
        description="Class names to create or modify (e.g., 'DataProcessor')"
    )
    code_snippets: list[str] = Field(
        default_factory=list,
        description="Relevant code fragments for context"
    )
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
    error_type: str | None = None


class ValidationVerdict(BaseModel):
    """Verdict returned by the validator after step execution.

    Levels 2-5 produce it via output_format; level 1 constructs it
    directly from subprocess.run exit code.
    """

    passed: bool
    diagnosis: str = ""
    suggested_fixes: list[str] = Field(default_factory=list)


class RefinementVerdict(BaseModel):
    """Verdict returned by the plan refiner on submit_plan.

    The refiner evaluates the plan against a rubric and
    returns either approval or rejection with actionable
    recommendations.
    """

    approved: bool
    score: int = Field(
        ge=0,
        le=100,
        description=(
            "Overall plan quality score (0-100). "
            "Plans below the configured threshold "
            "are rejected."
        ),
    )
    diagnosis: str = ""
    recommendations: list[str] = Field(
        default_factory=list,
    )
    rubric_scores: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per-dimension rubric scores. "
            "Keys are dimension names, values 0-5."
        ),
    )
    suggested_splits: list[str] = Field(
        default_factory=list,
        description=(
            "Step IDs that are too complex and should be "
            "split into multiple smaller steps."
        ),
    )


class ContextResult(BaseModel):
    """Enriched context produced by the context builder agent.

    Returned by build_context() and prepended to the executor's
    prev_context so the executor has precise, repo-grounded
    information about the code it needs to modify.
    """

    enriched_context: str = Field(
        description=(
            "Markdown-formatted context block with "
            "function signatures, imports, file "
            "dependencies, and other relevant info."
        ),
    )
    files_read: list[str] = Field(
        default_factory=list,
        description=(
            "Files that were read during context "
            "building, for traceability."
        ),
    )
    functions_found: list[str] = Field(
        default_factory=list,
        description=(
            "Function/class signatures discovered "
            "in the target files."
        ),
    )
    notes: str = ""


def sdk_output_schema(
    model_cls: type[BaseModel],
) -> dict[str, object]:
    """Build a JSON schema dict for Claude SDK output_format.

    Strips Pydantic-internal keys (title, $defs) that the
    SDK does not expect, keeping only type-relevant fields.

    Args:
        model_cls: A Pydantic BaseModel subclass.

    Returns:
        Dict suitable for ``ClaudeAgentOptions(
            output_format={"type": "json_schema",
                           "schema": result})``.
    """
    raw = model_cls.model_json_schema()
    stripped: dict[str, object] = {}
    for key, value in raw.items():
        if key in ("title", "$defs"):
            continue
        stripped[key] = value
    return stripped
