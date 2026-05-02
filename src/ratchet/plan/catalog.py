"""In-process MCP server exposing ratchet catalog tools to the planner.

All tools access shared state through ContextVars that are set once by the
orchestrator before starting a planner session. No module-level mutable state.
"""

import json
import logging
from contextvars import ContextVar
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool

from ratchet.plan.schema import (
    Step,
    StepIntent,
    StepType,
    Task,
    TaskCategory,
    TaskDescription,
    TaskStatus,
    ValidatorSpec,
)
from ratchet.plan.store import (
    PlanStore,
    RatchetStoreError,
    TaskStore,
)
from ratchet.state import State

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ContextVars -- module-level constants, values bound per-session by orchestrator
# ---------------------------------------------------------------------------

_task_store: ContextVar[TaskStore] = ContextVar("_task_store")
_plan_store: ContextVar[PlanStore] = ContextVar("_plan_store")
_state: ContextVar[State] = ContextVar("_state")
_repo_path: ContextVar[str] = ContextVar("_repo_path")


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
            "TaskStore ContextVar not set -- call bind_catalog_context() "
            "before starting a planner session."
        ) from exc


def _get_plan_store() -> PlanStore:
    """Retrieve PlanStore from ContextVar.

    Returns:
        The active PlanStore for this execution context.

    Raises:
        ContextVarNotSetError: If the orchestrator has not bound the store.
    """
    try:
        return _plan_store.get()
    except LookupError as exc:
        raise ContextVarNotSetError(
            "PlanStore ContextVar not set -- call bind_catalog_context() "
            "before starting a planner session."
        ) from exc


def _ok(data: Any) -> dict[str, Any]:
    """Wrap serialisable data in an MCP success response."""
    return {"content": [{"type": "text", "text": json.dumps(data, default=str)}]}


def _err(exc: Exception) -> dict[str, Any]:
    """Wrap an exception in an MCP error response."""
    return {
        "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
        "is_error": True,
    }


def _build_validator_spec(args: dict[str, Any]) -> ValidatorSpec:
    """Construct a ValidatorSpec from tool arguments."""
    return ValidatorSpec(
        level=args["validation_level"],
        success_criterion=args["success_criterion"],
        command=args.get("validator_command"),
        extra_context=args.get("validator_context"),
    )


def _build_step_from_args(args: dict[str, Any]) -> Step:
    """Build a Step from add_*_step tool arguments."""
    common: dict[str, Any] = {
        "id": args["id"],
        "type": StepType(args["step_type"]),
        "goal": args["goal"],
        "briefing": args["briefing"],
        "depends_on": args.get("depends_on", []),
        "validator": _build_validator_spec(args),
    }
    intent_raw: str | None = args.get("intent")
    if intent_raw is not None:
        common["intent"] = StepIntent(intent_raw)

    step_type = StepType(args["step_type"])

    if step_type in (StepType.IMPLEMENT_STEP, StepType.UPDATE_DOCS_STEP):
        common["target_files"] = args.get("target_files", [])
        common["creates_files"] = args.get("creates_files", [])

    if step_type == StepType.IMPLEMENT_STEP:
        common["deletes_files"] = args.get("deletes_files", [])

    return Step.model_validate(common)


# ---------------------------------------------------------------------------
# Tool input schemas
# ---------------------------------------------------------------------------

# --- Task schemas ---

_TASK_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Short human-readable label (max 200 chars).",
        },
        "description": {
            "type": "string",
            "description": "Full problem statement or issue text.",
        },
        "category": {
            "type": "string",
            "enum": [c.value for c in TaskCategory],
            "description": "Task category.",
        },
        "repo_path": {
            "type": "string",
            "description": "Absolute path to the repository.",
        },
        "issue_id": {
            "type": "string",
            "description": "Optional SWE-bench or tracker reference.",
        },
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
        "title": {
            "type": "string",
            "description": "New title (omit to leave unchanged).",
        },
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

# --- Plan: common step fields used in all add_*_step schemas ---

_STEP_COMMON_PROPERTIES: dict[str, Any] = {
    "id": {"type": "string", "description": "Unique step identifier."},
    "goal": {
        "type": "string",
        "description": "Short description of what the step must accomplish.",
    },
    "briefing": {
        "type": "string",
        "description": "Detailed instructions for the executor agent.",
    },
    "depends_on": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Step IDs that must complete before this step.",
    },
    "success_criterion": {
        "type": "string",
        "description": "Human-readable pass/fail condition.",
    },
    "validation_level": {
        "type": "integer",
        "minimum": 1,
        "maximum": 5,
        "description": (
            "Validation depth (1=subprocess, 2-5=LLM with"
            " increasing rigor)."
        ),
    },
    "validator_command": {
        "type": "string",
        "description": "Shell command for level 1 validation.",
    },
    "validator_context": {
        "type": "string",
        "description": "Additional context for levels 2-5 validation.",
    },
    "intent": {
        "type": "string",
        "enum": [i.value for i in StepIntent],
        "description": "Semantic intent hint.",
    },
}

_STEP_COMMON_REQUIRED: list[str] = [
    "id",
    "goal",
    "briefing",
    "success_criterion",
    "validation_level",
]

# --- Plan: per-type schemas ---

_ADD_DISCOVERY_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step_type": {"type": "string", "const": "discovery_step"},
        **_STEP_COMMON_PROPERTIES,
    },
    "required": ["step_type", *_STEP_COMMON_REQUIRED],
}

_ADD_IMPLEMENT_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step_type": {"type": "string", "const": "implement_step"},
        "target_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Existing files this step will modify.",
        },
        "creates_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "New files this step will create.",
        },
        "deletes_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Files this step will delete.",
        },
        **_STEP_COMMON_PROPERTIES,
    },
    "required": ["step_type", *_STEP_COMMON_REQUIRED],
}

_ADD_SIMPLE_TASK_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step_type": {"type": "string", "const": "simple_task_step"},
        "command": {
            "type": "string",
            "description": "Shell command to execute.",
        },
        **_STEP_COMMON_PROPERTIES,
    },
    "required": ["step_type", *_STEP_COMMON_REQUIRED],
}

_ADD_VERIFY_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step_type": {"type": "string", "const": "verify_step"},
        **_STEP_COMMON_PROPERTIES,
    },
    "required": ["step_type", *_STEP_COMMON_REQUIRED],
}

_ADD_UPDATE_DOCS_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step_type": {"type": "string", "const": "update_docs_step"},
        "target_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Existing documentation files to modify.",
        },
        "creates_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "New documentation files to create.",
        },
        **_STEP_COMMON_PROPERTIES,
    },
    "required": ["step_type", *_STEP_COMMON_REQUIRED],
}

_EDIT_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step_id": {"type": "string", "description": "ID of the step to edit."},
        "updates": {
            "type": "object",
            "description": "Fields to update on the step.",
            "properties": {
                "goal": {"type": "string"},
                "briefing": {"type": "string"},
                "target_files": {"type": "array", "items": {"type": "string"}},
                "creates_files": {"type": "array", "items": {"type": "string"}},
                "deletes_files": {"type": "array", "items": {"type": "string"}},
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "intent": {"type": "string", "enum": [i.value for i in StepIntent]},
            },
        },
    },
    "required": ["step_id", "updates"],
}

_REMOVE_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step_id": {"type": "string", "description": "ID of the step to remove."},
    },
    "required": ["step_id"],
}

_INSERT_STEP_AFTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "anchor_id": {
            "type": "string",
            "description": "ID of the step after which to insert.",
        },
        "step_type": {
            "type": "string",
            "enum": [t.value for t in StepType],
            "description": "Type of the new step.",
        },
        "step_args": {
            "type": "object",
            "description": "Arguments for the new step.",
            "properties": {
                "id": {"type": "string"},
                "goal": {"type": "string"},
                "briefing": {"type": "string"},
                "target_files": {"type": "array", "items": {"type": "string"}},
                "creates_files": {"type": "array", "items": {"type": "string"}},
                "deletes_files": {"type": "array", "items": {"type": "string"}},
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "success_criterion": {"type": "string"},
                "validation_level": {"type": "integer", "minimum": 1, "maximum": 5},
                "validator_command": {"type": "string"},
                "validator_context": {"type": "string"},
                "intent": {"type": "string"},
                "command": {"type": "string"},
            },
            "required": [
                "id",
                "goal",
                "briefing",
                "success_criterion",
                "validation_level",
            ],
        },
    },
    "required": ["anchor_id", "step_type", "step_args"],
}

_VIEW_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
}

_SUBMIT_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rationale": {
            "type": "string",
            "description": "Planner's explanation of the plan.",
        },
    },
    "required": ["rationale"],
}


# ---------------------------------------------------------------------------
# Task tool handlers
# ---------------------------------------------------------------------------

_DESCRIPTION_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "description",
        "category",
        "repo_path",
        "issue_id",
        "acceptance_criteria",
        "notes",
    }
)


@tool(
    "task_create",
    "Create a new task with a description and acceptance criteria.",
    _TASK_CREATE_SCHEMA,
)
async def _task_create_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle task_create tool calls."""
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
    """Handle task_get tool calls."""
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
    """Handle task_list tool calls."""
    try:
        store = _get_task_store()
        tasks = await store.list_all()
        return _ok([t.model_dump(mode="json") for t in tasks])
    except (RatchetCatalogError, RatchetStoreError) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in task_list")
        return _err(exc)


@tool(
    "task_update", "Update task fields or transition its status.", _TASK_UPDATE_SCHEMA
)
async def _task_update_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle task_update tool calls."""
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
                merged[field] = (
                    TaskCategory(args[field]) if field == "category" else args[field]
                )
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
    """Handle task_delete tool calls."""
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
# Plan tool handlers
# ---------------------------------------------------------------------------


@tool(
    "add_discovery_step",
    "Append a discovery step to the plan. No file declarations.",
    _ADD_DISCOVERY_STEP_SCHEMA,
)
async def _add_discovery_step_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle add_discovery_step tool calls."""
    try:
        store = _get_plan_store()
        step = _build_step_from_args(args)
        await store.add_step(step)
        return _ok(step.model_dump(mode="json"))
    except (RatchetCatalogError, RatchetStoreError, ValueError) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in add_discovery_step")
        return _err(exc)


@tool(
    "add_implement_step",
    "Append an implement step to the plan with file declarations.",
    _ADD_IMPLEMENT_STEP_SCHEMA,
)
async def _add_implement_step_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle add_implement_step tool calls."""
    try:
        store = _get_plan_store()
        step = _build_step_from_args(args)
        await store.add_step(step)
        return _ok(step.model_dump(mode="json"))
    except (RatchetCatalogError, RatchetStoreError, ValueError) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in add_implement_step")
        return _err(exc)


@tool(
    "add_simple_task_step",
    "Append a simple task step to the plan.",
    _ADD_SIMPLE_TASK_STEP_SCHEMA,
)
async def _add_simple_task_step_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle add_simple_task_step tool calls."""
    try:
        store = _get_plan_store()
        step = _build_step_from_args(args)
        await store.add_step(step)
        return _ok(step.model_dump(mode="json"))
    except (RatchetCatalogError, RatchetStoreError, ValueError) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in add_simple_task_step")
        return _err(exc)


@tool(
    "add_verify_step",
    "Append a verify step to the plan.",
    _ADD_VERIFY_STEP_SCHEMA,
)
async def _add_verify_step_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle add_verify_step tool calls."""
    try:
        store = _get_plan_store()
        step = _build_step_from_args(args)
        await store.add_step(step)
        return _ok(step.model_dump(mode="json"))
    except (RatchetCatalogError, RatchetStoreError, ValueError) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in add_verify_step")
        return _err(exc)


@tool(
    "add_update_docs_step",
    "Append an update docs step to the plan with file declarations.",
    _ADD_UPDATE_DOCS_STEP_SCHEMA,
)
async def _add_update_docs_step_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle add_update_docs_step tool calls."""
    try:
        store = _get_plan_store()
        step = _build_step_from_args(args)
        await store.add_step(step)
        return _ok(step.model_dump(mode="json"))
    except (RatchetCatalogError, RatchetStoreError, ValueError) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in add_update_docs_step")
        return _err(exc)


@tool(
    "edit_step",
    "Update fields on a PENDING or FAILED step. FAILED resets to PENDING.",
    _EDIT_STEP_SCHEMA,
)
async def _edit_step_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle edit_step tool calls."""
    try:
        store = _get_plan_store()
        step_id: str = args["step_id"]
        updates: dict[str, Any] = args["updates"]

        # Parse nested validator updates if present
        validator_updates: dict[str, Any] = {}
        clean_updates: dict[str, Any] = {}
        for key, value in updates.items():
            if key.startswith("validator_"):
                field_map = {
                    "validator_command": "command",
                    "validator_context": "extra_context",
                }
                if key in field_map:
                    validator_updates[field_map[key]] = value
            else:
                clean_updates[key] = value

        # Handle intent enum conversion
        if "intent" in clean_updates and isinstance(clean_updates["intent"], str):
            clean_updates["intent"] = StepIntent(clean_updates["intent"])

        # If any validator fields were provided, merge with existing
        if validator_updates:
            current = await store.get_step(step_id)
            current_validator = current.validator.model_dump()
            current_validator.update(validator_updates)
            clean_updates["validator"] = ValidatorSpec.model_validate(current_validator)

        step = await store.edit_step(step_id, clean_updates)
        return _ok(step.model_dump(mode="json"))
    except (RatchetCatalogError, RatchetStoreError, ValueError) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in edit_step")
        return _err(exc)


@tool(
    "remove_step",
    "Delete a PENDING or FAILED step from the plan.",
    _REMOVE_STEP_SCHEMA,
)
async def _remove_step_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle remove_step tool calls."""
    try:
        store = _get_plan_store()
        step_id: str = args["step_id"]
        await store.remove_step(step_id)
        return _ok({"removed": step_id})
    except (RatchetCatalogError, RatchetStoreError) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in remove_step")
        return _err(exc)


@tool(
    "insert_step_after",
    "Insert a new step immediately after the anchor step.",
    _INSERT_STEP_AFTER_SCHEMA,
)
async def _insert_step_after_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle insert_step_after tool calls."""
    try:
        store = _get_plan_store()
        anchor_id: str = args["anchor_id"]
        step_type: str = args["step_type"]
        step_args: dict[str, Any] = args["step_args"]

        full_args = {"step_type": step_type, **step_args}
        step = _build_step_from_args(full_args)
        await store.insert_step_after(anchor_id, step)
        return _ok(step.model_dump(mode="json"))
    except (RatchetCatalogError, RatchetStoreError, ValueError) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in insert_step_after")
        return _err(exc)


@tool(
    "view_plan",
    "Return the current plan (steps, statuses, outputs, verdicts).",
    _VIEW_PLAN_SCHEMA,
)
async def _view_plan_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle view_plan tool calls."""
    try:
        store = _get_plan_store()
        snapshot = await store.view()
        return _ok(snapshot)
    except (RatchetCatalogError, RatchetStoreError) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in view_plan")
        return _err(exc)


@tool(
    "submit_plan",
    "Mark the plan as submitted. Required before any step execution.",
    _SUBMIT_PLAN_SCHEMA,
)
async def _submit_plan_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handle submit_plan tool calls."""
    try:
        store = _get_plan_store()
        rationale: str = args["rationale"]
        await store.submit(rationale)
        return _ok({"submitted": True, "rationale": rationale})
    except (RatchetCatalogError, RatchetStoreError) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in submit_plan")
        return _err(exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

#: Names of all Task CRUD tools.
TASK_TOOL_NAMES: list[str] = [
    "task_create",
    "task_get",
    "task_list",
    "task_update",
    "task_delete",
]

#: Names of all Plan tools (excluding the step execution tool).
PLAN_TOOL_NAMES: list[str] = [
    "add_discovery_step",
    "add_implement_step",
    "add_simple_task_step",
    "add_verify_step",
    "add_update_docs_step",
    "edit_step",
    "remove_step",
    "insert_step_after",
    "view_plan",
    "submit_plan",
]

#: All catalog tool names for use in ClaudeAgentOptions.allowed_tools.
ALL_TOOL_NAMES: list[str] = TASK_TOOL_NAMES + PLAN_TOOL_NAMES

_ALL_HANDLERS = [
    # Task tools
    _task_create_handler,
    _task_get_handler,
    _task_list_handler,
    _task_update_handler,
    _task_delete_handler,
    # Plan tools
    _add_discovery_step_handler,
    _add_implement_step_handler,
    _add_simple_task_step_handler,
    _add_verify_step_handler,
    _add_update_docs_step_handler,
    _edit_step_handler,
    _remove_step_handler,
    _insert_step_after_handler,
    _view_plan_handler,
    _submit_plan_handler,
]


def build_catalog_server() -> McpSdkServerConfig:
    """Build the ratchet_catalog in-process MCP server.

    Registers all Task CRUD and Plan tools. Call bind_catalog_context()
    before starting a planner session to inject the required stores.

    Returns:
        McpSdkServerConfig for use in ClaudeAgentOptions.mcp_servers.
    """
    return create_sdk_mcp_server("ratchet_catalog", tools=_ALL_HANDLERS)


def bind_catalog_context(
    task_store: TaskStore,
    plan_store: PlanStore,
    state: State,
    repo_path: str,
) -> None:
    """Bind ContextVars for ratchet_catalog tool handlers.

    Must be called by the orchestrator in the same asyncio task (or a parent
    whose context is inherited) before the planner session starts.

    Args:
        task_store: The TaskStore instance for this solve session.
        plan_store: The PlanStore instance for this solve session.
        state: The State instance for recording step outputs.
        repo_path: Absolute path to the target repository.
    """
    _task_store.set(task_store)
    _plan_store.set(plan_store)
    _state.set(state)
    _repo_path.set(repo_path)
