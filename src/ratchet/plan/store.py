"""Asyncio-locked in-memory stores for ratchet plan entities.

No SDK calls. No business logic beyond state transitions and constraints.
"""

import asyncio
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from ratchet.plan.schema import (
    Step,
    StepOutput,
    Task,
    TaskDescription,
    TaskStatus,
    ValidationVerdict,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class RatchetStoreError(Exception):
    """Base class for all store-layer errors."""


class TaskNotFoundError(RatchetStoreError):
    """Raised when a requested task does not exist in the store."""


class StepNotFoundError(RatchetStoreError):
    """Raised when a requested step does not exist in the plan."""


class InvalidStatusTransitionError(RatchetStoreError):
    """Raised when a requested status transition is not permitted."""


class StepImmutableError(RatchetStoreError):
    """Raised when a mutation is attempted on a step in a protected state."""


class DuplicateStepIdError(RatchetStoreError):
    """Raised when adding a step with an id that already exists."""


class PlanNotSubmittableError(RatchetStoreError):
    """Raised when submit is called on an empty plan."""


class TaskImmutableError(RatchetStoreError):
    """Raised when a mutation is attempted on a task in a terminal or locked state."""


# ---------------------------------------------------------------------------
# StepStatus
# ---------------------------------------------------------------------------


class StepStatus(StrEnum):
    """Status of a single step within the plan."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Task status machine constants
# ---------------------------------------------------------------------------

_TASK_TRANSITIONS: Final[dict[TaskStatus, frozenset[TaskStatus]]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED}),
    TaskStatus.IN_PROGRESS: frozenset(
        {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

_TASK_TERMINAL: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

_TASK_LOCKED: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED}
)


# ---------------------------------------------------------------------------
# TaskStore
# ---------------------------------------------------------------------------


class TaskStore:
    """Asyncio-locked in-memory store for Task entities.

    All public methods are coroutines that acquire the internal lock for the
    duration of the operation. Every read/write returns a deep copy so callers
    cannot accidentally mutate store state through a returned object.
    """

    def __init__(self) -> None:
        """Initialise an empty TaskStore."""
        self._lock: asyncio.Lock = asyncio.Lock()
        self._tasks: dict[str, Task] = {}

    async def create(self, description: TaskDescription) -> Task:
        """Create and persist a new Task from a TaskDescription.

        Args:
            description: Planner-supplied task fields.

        Returns:
            A deep copy of the newly created Task (status=pending).
        """
        async with self._lock:
            task = Task.from_description(description)
            self._tasks[task.id] = task
            logger.debug("TaskStore.create id=%s", task.id)
            return task.model_copy(deep=True)

    async def get(self, task_id: str) -> Task:
        """Retrieve a task by its hex UUID.

        Args:
            task_id: The task's hex UUID.

        Returns:
            A deep copy of the stored Task.

        Raises:
            TaskNotFoundError: If no task with that ID exists.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise TaskNotFoundError(f"Task not found: {task_id!r}")
            return task.model_copy(deep=True)

    async def update_status(self, task_id: str, status: TaskStatus) -> Task:
        """Transition a task to a new status.

        Args:
            task_id: The task's hex UUID.
            status: The target status.

        Returns:
            A deep copy of the updated Task.

        Raises:
            TaskNotFoundError: If no task with that ID exists.
            InvalidStatusTransitionError: If the transition is not permitted.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise TaskNotFoundError(f"Task not found: {task_id!r}")
            if status not in _TASK_TRANSITIONS[task.status]:
                raise InvalidStatusTransitionError(
                    f"Cannot transition task {task_id!r} from "
                    f"{task.status!r} to {status!r}"
                )
            task.status = status
            task.updated_at = datetime.now(UTC)
            logger.debug("TaskStore.update_status id=%s status=%s", task_id, status)
            return task.model_copy(deep=True)

    async def update_description(
        self, task_id: str, description: TaskDescription
    ) -> Task:
        """Replace the mutable description fields of a task.

        Args:
            task_id: The task's hex UUID.
            description: New description fields to apply.

        Returns:
            A deep copy of the updated Task.

        Raises:
            TaskNotFoundError: If no task with that ID exists.
            TaskImmutableError: If the task has a terminal status.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise TaskNotFoundError(f"Task not found: {task_id!r}")
            if task.status in _TASK_TERMINAL:
                raise TaskImmutableError(
                    f"Cannot update description of task {task_id!r} "
                    f"with terminal status {task.status!r}"
                )
            for field, value in description.model_dump().items():
                setattr(task, field, value)
            task.updated_at = datetime.now(UTC)
            logger.debug("TaskStore.update_description id=%s", task_id)
            return task.model_copy(deep=True)

    async def delete(self, task_id: str) -> None:
        """Remove a task from the store.

        Args:
            task_id: The task's hex UUID.

        Raises:
            TaskNotFoundError: If no task with that ID exists.
            TaskImmutableError: If the task is in_progress or completed.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise TaskNotFoundError(f"Task not found: {task_id!r}")
            if task.status in _TASK_LOCKED:
                raise TaskImmutableError(
                    f"Cannot delete task {task_id!r} with status {task.status!r}"
                )
            del self._tasks[task_id]
            logger.debug("TaskStore.delete id=%s", task_id)

    async def list_all(self) -> list[Task]:
        """Return all tasks sorted by creation time ascending.

        Returns:
            List of deep-copied Tasks sorted by created_at.
        """
        async with self._lock:
            return sorted(
                (t.model_copy(deep=True) for t in self._tasks.values()),
                key=lambda t: t.created_at,
            )


# ---------------------------------------------------------------------------
# PlanStore
# ---------------------------------------------------------------------------


class PlanStore:
    """Asyncio-locked in-memory store for the active plan.

    Holds an ordered list of Steps with per-step status, output, and verdict
    tracking. All mutations acquire the internal lock. The plan starts empty
    and is built by the planner through catalog tool calls.
    """

    def __init__(self) -> None:
        """Initialise an empty PlanStore."""
        self._lock: asyncio.Lock = asyncio.Lock()
        self._steps: list[Step] = []
        self._statuses: dict[str, StepStatus] = {}
        self._outputs: dict[str, StepOutput] = {}
        self._verdicts: dict[str, ValidationVerdict] = {}
        self._rationale: str = ""
        self._submitted: bool = False

    # -- helpers ----------------------------------------------------------

    def _find_step_index(self, step_id: str) -> int:
        """Return the index of a step by id.

        Raises:
            StepNotFoundError: If the step does not exist.
        """
        for idx, s in enumerate(self._steps):
            if s.id == step_id:
                return idx
        raise StepNotFoundError(f"Step not found: {step_id!r}")

    @staticmethod
    def _validate_step_status(
        step_id: str,
        current: StepStatus,
        required: StepStatus,
    ) -> None:
        """Raise if the step is not in the required status.

        Args:
            step_id: The step being checked.
            current: Current status of the step.
            required: The status required for the operation.

        Raises:
            StepImmutableError: If the current status does not match required.
        """
        if current != required:
            raise StepImmutableError(
                f"Step {step_id!r} is {current!r}, expected {required!r}"
            )

    # -- mutations --------------------------------------------------------

    async def add_step(self, step: Step) -> None:
        """Append a new step to the plan.

        Args:
            step: Fully constructed Step to add.

        Raises:
            DuplicateStepIdError: If a step with the same id already exists.
        """
        async with self._lock:
            if any(s.id == step.id for s in self._steps):
                raise DuplicateStepIdError(f"Step id already exists: {step.id!r}")
            self._steps.append(step)
            self._statuses[step.id] = StepStatus.PENDING
            logger.debug("PlanStore.add_step id=%s type=%s", step.id, step.type)

    async def edit_step(self, step_id: str, updates: dict[str, Any]) -> Step:
        """Update fields on a PENDING or FAILED step.

        Editing a FAILED step resets its status to PENDING.

        Args:
            step_id: The step to edit.
            updates: Dict of field names to new values.

        Returns:
            A deep copy of the updated Step.

        Raises:
            StepNotFoundError: If the step does not exist.
            StepImmutableError: If the step is IN_PROGRESS or COMPLETED.
        """
        async with self._lock:
            idx = self._find_step_index(step_id)
            step = self._steps[idx]
            status = self._statuses[step_id]
            if status in (StepStatus.IN_PROGRESS, StepStatus.COMPLETED):
                raise StepImmutableError(
                    f"Cannot edit step {step_id!r} with status {status!r}"
                )
            updated_data = step.model_dump()
            updated_data.update(updates)
            self._steps[idx] = Step.model_validate(updated_data)
            if status == StepStatus.FAILED:
                self._statuses[step_id] = StepStatus.PENDING
                self._verdicts.pop(step_id, None)
                logger.debug("PlanStore.edit_step id=%s (FAILED -> PENDING)", step_id)
            else:
                logger.debug("PlanStore.edit_step id=%s", step_id)
            return self._steps[idx].model_copy(deep=True)

    async def remove_step(self, step_id: str) -> None:
        """Delete a step from the plan.

        Removes the step and its associated status, output, and verdict
        entries.

        Args:
            step_id: The step to remove.

        Raises:
            StepNotFoundError: If the step does not exist.
            StepImmutableError: If the step is IN_PROGRESS.
        """
        async with self._lock:
            idx = self._find_step_index(step_id)
            status = self._statuses[step_id]
            if status == StepStatus.IN_PROGRESS:
                raise StepImmutableError(
                    f"Cannot remove step {step_id!r} with status "
                    f"{StepStatus.IN_PROGRESS!r}"
                )
            del self._steps[idx]
            self._statuses.pop(step_id, None)
            self._outputs.pop(step_id, None)
            self._verdicts.pop(step_id, None)
            logger.debug("PlanStore.remove_step id=%s", step_id)

    async def insert_step_after(self, anchor_id: str, step: Step) -> None:
        """Insert a new step immediately after the anchor step.

        Args:
            anchor_id: The step after which to insert.
            step: Fully constructed Step to insert.

        Raises:
            StepNotFoundError: If the anchor step does not exist.
            DuplicateStepIdError: If the new step id already exists.
        """
        async with self._lock:
            anchor_idx = self._find_step_index(anchor_id)
            if any(s.id == step.id for s in self._steps):
                raise DuplicateStepIdError(f"Step id already exists: {step.id!r}")
            insert_at = anchor_idx + 1
            self._steps.insert(insert_at, step)
            self._statuses[step.id] = StepStatus.PENDING
            logger.debug(
                "PlanStore.insert_step_after anchor=%s new_id=%s at=%d",
                anchor_id,
                step.id,
                insert_at,
            )

    # -- status transitions -----------------------------------------------

    async def mark_in_progress(self, step_id: str) -> None:
        """Transition a step from PENDING to IN_PROGRESS.

        Args:
            step_id: The step to mark.

        Raises:
            StepNotFoundError: If the step does not exist.
            StepImmutableError: If the step is not PENDING.
        """
        async with self._lock:
            self._find_step_index(step_id)  # existence check
            status = self._statuses[step_id]
            self._validate_step_status(step_id, status, StepStatus.PENDING)
            self._statuses[step_id] = StepStatus.IN_PROGRESS
            logger.debug("PlanStore.mark_in_progress id=%s", step_id)

    async def mark_completed(
        self,
        step_id: str,
        output: StepOutput,
        verdict: ValidationVerdict,
    ) -> None:
        """Transition a step from IN_PROGRESS to COMPLETED and record output.

        Args:
            step_id: The step to mark.
            output: Executor output for this step.
            verdict: Validator verdict for this step.

        Raises:
            StepNotFoundError: If the step does not exist.
            StepImmutableError: If the step is not IN_PROGRESS.
        """
        async with self._lock:
            self._find_step_index(step_id)  # existence check
            status = self._statuses[step_id]
            self._validate_step_status(step_id, status, StepStatus.IN_PROGRESS)
            self._statuses[step_id] = StepStatus.COMPLETED
            self._outputs[step_id] = output
            self._verdicts[step_id] = verdict
            logger.debug("PlanStore.mark_completed id=%s", step_id)

    async def mark_failed(
        self,
        step_id: str,
        verdict: ValidationVerdict,
    ) -> None:
        """Transition a step from IN_PROGRESS to FAILED and record verdict.

        Args:
            step_id: The step to mark.
            verdict: Validator verdict explaining the failure.

        Raises:
            StepNotFoundError: If the step does not exist.
            StepImmutableError: If the step is not IN_PROGRESS.
        """
        async with self._lock:
            self._find_step_index(step_id)  # existence check
            status = self._statuses[step_id]
            self._validate_step_status(step_id, status, StepStatus.IN_PROGRESS)
            self._statuses[step_id] = StepStatus.FAILED
            self._verdicts[step_id] = verdict
            logger.debug("PlanStore.mark_failed id=%s", step_id)

    # -- queries ----------------------------------------------------------

    async def get_step(self, step_id: str) -> Step:
        """Retrieve a step by its id.

        Args:
            step_id: The step to retrieve.

        Returns:
            A deep copy of the Step.

        Raises:
            StepNotFoundError: If the step does not exist.
        """
        async with self._lock:
            idx = self._find_step_index(step_id)
            return self._steps[idx].model_copy(deep=True)

    async def get_status(self, step_id: str) -> StepStatus:
        """Retrieve the status of a step.

        Args:
            step_id: The step to query.

        Returns:
            The current StepStatus.

        Raises:
            StepNotFoundError: If the step does not exist.
        """
        async with self._lock:
            self._find_step_index(step_id)  # existence check
            return self._statuses[step_id]

    async def get_output(self, step_id: str) -> StepOutput:
        """Retrieve the recorded output of a completed step.

        Args:
            step_id: The step to query.

        Returns:
            A deep copy of the StepOutput.

        Raises:
            StepNotFoundError: If the step does not exist.
            KeyError: If the step has no recorded output.
        """
        async with self._lock:
            self._find_step_index(step_id)
            return self._outputs[step_id].model_copy(deep=True)

    async def get_verdict(self, step_id: str) -> ValidationVerdict:
        """Retrieve the recorded verdict of a step.

        Args:
            step_id: The step to query.

        Returns:
            A deep copy of the ValidationVerdict.

        Raises:
            StepNotFoundError: If the step does not exist.
            KeyError: If the step has no recorded verdict.
        """
        async with self._lock:
            self._find_step_index(step_id)
            return self._verdicts[step_id].model_copy(deep=True)

    async def next_runnable_id(self) -> str | None:
        """Return the first PENDING step whose depends_on are all COMPLETED.

        Returns:
            The step id of the next runnable step, or None.
        """
        async with self._lock:
            for step in self._steps:
                if self._statuses[step.id] != StepStatus.PENDING:
                    continue
                if all(
                    self._statuses.get(dep) == StepStatus.COMPLETED
                    for dep in step.depends_on
                ):
                    return step.id
            return None

    async def view(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the full plan state.

        Returns:
            Dict with steps, statuses, outputs, verdicts, rationale,
            and submitted flag.
        """
        async with self._lock:
            return {
                "steps": [s.model_dump(mode="json") for s in self._steps],
                "statuses": {k: v.value for k, v in self._statuses.items()},
                "outputs": {
                    k: v.model_dump(mode="json") for k, v in self._outputs.items()
                },
                "verdicts": {
                    k: v.model_dump(mode="json") for k, v in self._verdicts.items()
                },
                "rationale": self._rationale,
                "submitted": self._submitted,
            }

    async def submit(self, rationale: str) -> None:
        """Mark the plan as submitted with a rationale.

        Submission is a flag, not a freeze -- the planner may still edit,
        add, or remove steps after submission.

        Args:
            rationale: Planner's explanation of the plan.

        Raises:
            PlanNotSubmittableError: If the plan has no steps.
        """
        async with self._lock:
            if not self._steps:
                raise PlanNotSubmittableError("Cannot submit an empty plan")
            self._rationale = rationale
            self._submitted = True
            logger.debug(
                "PlanStore.submit steps=%d rationale=%r",
                len(self._steps),
                rationale[:80],
            )
