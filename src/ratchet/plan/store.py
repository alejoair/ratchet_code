"""Asyncio-locked in-memory stores for ratchet plan entities.

No SDK calls. No business logic beyond state transitions and constraints.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Final

from ratchet.plan.schema import Task, TaskDescription, TaskStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class RatchetStoreError(Exception):
    """Base class for all store-layer errors."""


class TaskNotFoundError(RatchetStoreError):
    """Raised when a requested task does not exist in the store."""


class InvalidStatusTransitionError(RatchetStoreError):
    """Raised when a requested status transition is not permitted."""


class TaskImmutableError(RatchetStoreError):
    """Raised when a mutation is attempted on a task in a terminal or locked state."""


# ---------------------------------------------------------------------------
# Allowed status transitions
# ---------------------------------------------------------------------------

_TRANSITIONS: Final[dict[TaskStatus, frozenset[TaskStatus]]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED}),
    TaskStatus.IN_PROGRESS: frozenset(
        {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

_TERMINAL: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

_LOCKED: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED}
)


# ---------------------------------------------------------------------------
# TaskStore
# ---------------------------------------------------------------------------


class TaskStore:
    """Thread-safe (asyncio-locked) in-memory store for Task entities.

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
            if status not in _TRANSITIONS[task.status]:
                raise InvalidStatusTransitionError(
                    f"Cannot transition task {task_id!r} from "
                    f"{task.status!r} to {status!r}"
                )
            task.status = status
            task.updated_at = datetime.now(timezone.utc)
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
            if task.status in _TERMINAL:
                raise TaskImmutableError(
                    f"Cannot update description of task {task_id!r} "
                    f"with terminal status {task.status!r}"
                )
            for field, value in description.model_dump().items():
                setattr(task, field, value)
            task.updated_at = datetime.now(timezone.utc)
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
            if task.status in _LOCKED:
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
