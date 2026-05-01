"""In-process MCP server exposing ratchet catalog tools to the planner.

All tools access shared state through ContextVars that are set once by the
orchestrator before starting a planner session. No module-level mutable state.
"""

import json
import logging
from contextvars import ContextVar
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool

from ratchet.plan.schema import Task, TaskCategory, TaskDescription, TaskStatus
from ratchet.plan.store import (
    InvalidStatusTransitionError,
    RatchetStoreError,
    TaskImmutableError,
    TaskNotFoundError,
    TaskStore,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ContextVars — module-level constants, values bound per-session by orchestrator
# ---------------------------------------------------------------------------

_task_store: ContextVar[TaskStore] = ContextVar("_task_store")


# ---------------------------------------------------------------------------
# Catalog-layer exceptions
# ---------------------------------------------------------------------------


class RatchetCatalogError(Exception):
    """Base class for errors raised within ratchet_catalog tool handlers."""


class ContextVarNotSetError(RatchetCatalogError):
    """Raised when a required ContextVar has not been bound by the orchestrator."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_task_store() -> TaskStore:
    """Retrieve TaskStore from ContextVar.

    Returns:
        The active TaskStore for this execution context.

    Raises:
        ContextVarNotSetError: If the orchestrator has not bound the store.
    """
    try:
        return _task_store.get()
    except LookupError as exc:
        raise ContextVarNotSetError(
            "TaskStore ContextVar not set — call bind_catalog_context() "
            "before starting a planner session."
        ) from exc


def _ok(data: Any) -> dict[str, Any]:
    """Wrap serialisable data in an MCP success response.

    Args:
        data: Any JSON-serialisable value.

    Returns:
        MCP tool result dict with a single text content block.
    """
    return {"content": [{"type": "text", "text": json.dumps(data, default=str)}]}


def _err(exc: Exception) -> dict[str, Any]:
    """Wrap an exception in an MCP error response.

    Args:
        exc: The exception to surface to the model.

    Returns:
        MCP tool result dict with is_error=True.
    """
    return {
        "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
        "is_error": True,
    }


# ---------------------------------------------------------------------------
# Tool input schemas (full JSON Schema to control required vs optional fields)
# ---------------------------------------------------------------------------

_TASK_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short human-readable label (max 200 chars)."},
        "description": {"type": "string", "description": "Full problem statement or issue text."},
        "category": {
            "type": "string",
            "enum": [c.value for c in TaskCategory],
            "description": "Task category.",
        },
        "repo_path": {"type": "string", "description": "Absolute path to the repository."},
        "issue_id": {"type": "string", "description": "Optional SWE-bench or tracker reference."},
        "acceptance_criteria": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Verifiable conditions for task completion.",
        },
        "notes": {"type": "string", "description": "Free-form planner notes."},
    },
    "required": ["title", "description", "category", "repo_path"],
}

_TASK_GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "Hex UUID of the task."},
    },
    "required": ["task_id"],
}

_TASK_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
}

_TASK_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "Hex UUID of the task to update."},
        "title": {"type": "string", "description": "New title (omit to leave unchanged)."},
        "description": {"type": "string", "description": "New description text."},
        "category": {
            "type": "string",
            "enum": [c.value for c in TaskCategory],
            "description": "New category.",
        },
        "repo_path": {"type": "string", "description": "New repo path."},
        "issue_id": {"type": "string", "description": "New issue reference."},
        "acceptance_criteria": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Replacement acceptance criteria list.",
        },
        "notes": {"type": "string", "description": "New notes."},
        "status": {
            "type": "string",
            "enum": [s.value for s in TaskStatus],
            "description": "New status (triggers transition validation).",
        },
    },
    "required": ["task_id"],
}

_TASK_DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "Hex UUID of the task to delete."},
    },
    "required": ["task_id"],
}

# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

_DESCRIPTION_FIELDS: frozenset[str] = frozenset(
    {"title", "description", "category", "repo_path", "issue_id", "acceptance_criteria", "notes"}
)


@tool("task_create", "Create a new task with a description and acceptance criteria.", _TASK_CREATE_SCHEMA)
async def _task_create_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle task_create tool calls.

    Args:
        args: Tool input matching _TASK_CREATE_SCHEMA.

    Returns:
        MCP tool result containing Task JSON or an error block.
    """
    try:
        store = _get_task_store()
        desc = TaskDescription(
            title=args["title"],
            description=args["description"],
            category=TaskCategory(args["category"]),
            repo_path=args["repo_path"],
            issue_id=args.get("issue_id"),
            acceptance_criteria=args.get("acceptance_criteria", []),
            notes=args.get("notes"),
        )
        task = await store.create(desc)
        return _ok(task.model_dump(mode="json"))
    except (RatchetCatalogError, RatchetStoreError, ValueError) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in task_create")
        return _err(exc)


@tool("task_get", "Retrieve a task by its ID.", _TASK_GET_SCHEMA)
async def _task_get_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle task_get tool calls.

    Args:
        args: Tool input matching _TASK_GET_SCHEMA.

    Returns:
        MCP tool result containing Task JSON or an error block.
    """
    try:
        store = _get_task_store()
        task = await store.get(args["task_id"])
        return _ok(task.model_dump(mode="json"))
    except (RatchetCatalogError, RatchetStoreError) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in task_get")
        return _err(exc)


@tool("task_list", "List all tasks sorted by creation time.", _TASK_LIST_SCHEMA)
async def _task_list_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle task_list tool calls.

    Args:
        args: Tool input (empty).

    Returns:
        MCP tool result containing a JSON array of Tasks or an error block.
    """
    try:
        store = _get_task_store()
        tasks = await store.list_all()
        return _ok([t.model_dump(mode="json") for t in tasks])
    except (RatchetCatalogError, RatchetStoreError) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in task_list")
        return _err(exc)


@tool("task_update", "Update task fields or transition its status.", _TASK_UPDATE_SCHEMA)
async def _task_update_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle task_update tool calls.

    Applies description field changes first (if any), then the status
    transition (if provided). A call with only task_id is a no-op that
    returns the current task.

    Args:
        args: Tool input matching _TASK_UPDATE_SCHEMA.

    Returns:
        MCP tool result containing the updated Task JSON or an error block.
    """
    try:
        store = _get_task_store()
        task_id: str = args["task_id"]
        present_desc_fields = _DESCRIPTION_FIELDS & args.keys()
        new_status_raw: str | None = args.get("status")

        task: Task | None = None

        if present_desc_fields:
            current = await store.get(task_id)
            merged: dict[str, Any] = current.model_dump(
                include=_DESCRIPTION_FIELDS  # type: ignore[arg-type]
            )
            for field in present_desc_fields:
                merged[field] = TaskCategory(args[field]) if field == "category" else args[field]
            desc = TaskDescription.model_validate(merged)
            task = await store.update_description(task_id, desc)

        if new_status_raw is not None:
            task = await store.update_status(task_id, TaskStatus(new_status_raw))

        if task is None:
            task = await store.get(task_id)

        return _ok(task.model_dump(mode="json"))
    except (RatchetCatalogError, RatchetStoreError, ValueError) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in task_update")
        return _err(exc)


@tool("task_delete", "Delete a task by its ID.", _TASK_DELETE_SCHEMA)
async def _task_delete_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle task_delete tool calls.

    Args:
        args: Tool input matching _TASK_DELETE_SCHEMA.

    Returns:
        MCP tool result with confirmation JSON or an error block.
    """
    try:
        store = _get_task_store()
        await store.delete(args["task_id"])
        return _ok({"deleted": args["task_id"]})
    except (RatchetCatalogError, RatchetStoreError) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in task_delete")
        return _err(exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

#: Names of all Task CRUD tools, for use in ClaudeAgentOptions.allowed_tools.
TASK_TOOL_NAMES: list[str] = [
    "task_create",
    "task_get",
    "task_list",
    "task_update",
    "task_delete",
]


def build_catalog_server() -> McpSdkServerConfig:
    """Build the ratchet_catalog in-process MCP server.

    Registers all Task CRUD tools. Call bind_catalog_context() before
    starting a planner session to inject the required stores.

    Returns:
        McpSdkServerConfig for use in ClaudeAgentOptions.mcp_servers.
    """
    return create_sdk_mcp_server(
        "ratchet_catalog",
        tools=[
            _task_create_handler,
            _task_get_handler,
            _task_list_handler,
            _task_update_handler,
            _task_delete_handler,
        ],
    )


def bind_catalog_context(task_store: TaskStore) -> None:
    """Bind ContextVars for ratchet_catalog tool handlers.

    Must be called by the orchestrator in the same asyncio task (or a parent
    whose context is inherited) before the planner session starts.

    Args:
        task_store: The TaskStore instance for this solve session.
    """
    _task_store.set(task_store)
