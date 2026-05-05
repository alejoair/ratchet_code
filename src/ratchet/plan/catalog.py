"""In-process MCP server exposing ratchet catalog tools to the planner.

All tools access shared state through ContextVars that are set once by the
orchestrator before starting a planner session. No module-level mutable state.
"""

import json
import logging
import os
from contextvars import ContextVar
from typing import Any

from claude_agent_sdk import (
    McpSdkServerConfig,
    create_sdk_mcp_server,
    tool,
)

from ratchet.config import Config
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
    StepStatus,
    TaskStore,
)
from ratchet.state import State

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ContextVars
# ---------------------------------------------------------------------------

_task_store: ContextVar[TaskStore] = ContextVar(
    "_task_store",
)
_plan_store: ContextVar[PlanStore] = ContextVar(
    "_plan_store",
)
_state: ContextVar[State] = ContextVar("_state")
_repo_path: ContextVar[str] = ContextVar(
    "_repo_path",
)
_config: ContextVar[Config] = ContextVar("_config")
_planner_context: ContextVar["PlannerContextStore"] = ContextVar(
    "_planner_context",
)


# ---------------------------------------------------------------------------
# Catalog-layer exceptions
# ---------------------------------------------------------------------------


class RatchetCatalogError(Exception):
    """Base for ratchet_catalog tool errors."""


class ContextVarNotSetError(RatchetCatalogError):
    """Raised when a ContextVar is not bound."""


class PrerequisitesNotMetError(RatchetCatalogError):
    """Raised when prerequisite checks fail."""


# ---------------------------------------------------------------------------
# Planner context store
# ---------------------------------------------------------------------------


class PlannerContextStore:
    """Simple string store for dynamic planner context.

    Provides append/replace/clear operations for context that
    persists across planner turns without modifying CLAUDE.md.
    """
    def __init__(self) -> None:
        self._context: str = ""

    def get(self) -> str:
        """Return the current context string."""
        return self._context

    def set(self, content: str, mode: str = "replace") -> str:
        """Update context content.

        Args:
            content: New context content.
            mode: 'replace' to overwrite, 'append' to add to end,
                  'clear' to empty then set.

        Returns:
            The updated context string.
        """
        if mode == "clear":
            self._context = content
        elif mode == "append":
            if self._context:
                self._context += "\n\n" + content
            else:
                self._context = content
        else:  # replace (default)
            self._context = content
        return self._context


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_task_store() -> TaskStore:
    """Retrieve TaskStore from ContextVar."""
    try:
        return _task_store.get()
    except LookupError as exc:
        raise ContextVarNotSetError(
            "TaskStore not set."
        ) from exc


def _get_plan_store() -> PlanStore:
    """Retrieve PlanStore from ContextVar."""
    try:
        return _plan_store.get()
    except LookupError as exc:
        raise ContextVarNotSetError(
            "PlanStore not set."
        ) from exc


def _get_state() -> State:
    """Retrieve State from ContextVar."""
    try:
        return _state.get()
    except LookupError as exc:
        raise ContextVarNotSetError(
            "State not set."
        ) from exc


def _get_repo_path() -> str:
    """Retrieve repo_path from ContextVar."""
    try:
        return _repo_path.get()
    except LookupError as exc:
        raise ContextVarNotSetError(
            "repo_path not set."
        ) from exc


def _get_config() -> Config:
    """Retrieve Config from ContextVar."""
    try:
        return _config.get()
    except LookupError as exc:
        raise ContextVarNotSetError(
            "Config not set."
        ) from exc


def _get_planner_context() -> PlannerContextStore:
    """Retrieve PlannerContextStore from ContextVar."""
    try:
        return _planner_context.get()
    except LookupError as exc:
        raise ContextVarNotSetError(
            "PlannerContextStore not set."
        ) from exc


def _ok(data: Any) -> dict[str, Any]:
    """Wrap data in an MCP success response."""
    return {
        "content": [{
            "type": "text",
            "text": json.dumps(data, default=str),
        }],
    }


def _err(exc: Exception) -> dict[str, Any]:
    """Wrap an exception in an MCP error response."""
    return {
        "content": [{
            "type": "text",
            "text": (
                f"{type(exc).__name__}: {exc}"
            ),
        }],
        "is_error": True,
    }


# ---------------------------------------------------------------------------
# check_prerequisites
# ---------------------------------------------------------------------------


async def check_prerequisites(
    step: Step,
    store: PlanStore,
    rp: str,
    cfg: Config,
) -> list[str]:
    """Validate prerequisites before executing a step.

    Checks:
      1. depends_on entries exist and are COMPLETED.
      2. For implement/update_docs: target_files exist,
         creates_files do not exist, no duplicates.
      3. Step status is PENDING or FAILED.
      4. Planning rules from config are satisfied
         (e.g. require_discovery_before).

    Args:
        step: The step to validate.
        store: The active PlanStore.
        cfg: Active configuration with planning_rules.

    Returns:
        List of error strings (empty if all pass).
    """
    errors: list[str] = []

    # Check dependency statuses
    for dep_id in step.depends_on:
        try:
            dep_status = await store.get_status(dep_id)
            if dep_status != StepStatus.COMPLETED:
                errors.append(
                    f"Dependency {dep_id!r} is "
                    f"{dep_status!r}, not COMPLETED"
                )
        except Exception as exc:
            errors.append(
                f"Dependency {dep_id!r} not found: "
                f"{exc}"
            )

    # File existence checks for write-capable types
    if step.type in (
        StepType.IMPLEMENT_STEP,
        StepType.UPDATE_DOCS_STEP,
    ):
        all_files = (
            step.target_files + step.creates_files
        )
        if step.type == StepType.IMPLEMENT_STEP:
            all_files = (
                step.target_files
                + step.creates_files
                + step.deletes_files
            )

        # Check for duplicates across lists
        seen: set[str] = set()
        for f in all_files:
            if f in seen:
                errors.append(
                    f"File {f!r} in multiple lists"
                )
            seen.add(f)

        for f in step.target_files:
            full = os.path.join(rp, f)
            if not os.path.exists(full):
                errors.append(
                    f"target_file {f!r} missing"
                )

        for f in step.creates_files:
            full = os.path.join(rp, f)
            if os.path.exists(full):
                errors.append(
                    f"creates_file {f!r} exists"
                )

        if step.type == StepType.IMPLEMENT_STEP:
            for f in step.deletes_files:
                full = os.path.join(rp, f)
                if not os.path.exists(full):
                    errors.append(
                        f"deletes_file {f!r} missing"
                    )

    # Step must be PENDING or FAILED (FAILED will be
    # auto-reset to PENDING by plan_executor.run_step).
    status = await store.get_status(step.id)
    if status not in (
        StepStatus.PENDING, StepStatus.FAILED,
    ):
        errors.append(
            f"Step {step.id!r} is {status!r}, "
            f"expected PENDING or FAILED"
        )

    # Planning rules: require_discovery_before
    rules = cfg.planning_rules
    if step.type.value in rules.require_discovery_before:
        has_discovery_dep = False
        for dep_id in step.depends_on:
            try:
                dep = await store.get_step(dep_id)
                if (
                    dep.type
                    == StepType.DISCOVERY_STEP
                ):
                    has_discovery_dep = True
            except Exception:
                pass
        if not has_discovery_dep:
            errors.append(
                f"Step type {step.type.value!r} "
                f"requires a discovery_step "
                f"dependency (planning_rules)"
            )

    return errors


async def _validate_plan_rules(
    store: PlanStore,
    cfg: Config,
) -> list[str]:
    """Validate the full plan against planning rules.

    Called by submit_plan to catch structural issues before
    the planner starts executing steps.

    Args:
        store: The active PlanStore.
        cfg: Active configuration with planning_rules.

    Returns:
        List of error strings (empty if all pass).
    """
    errors: list[str] = []
    rules = cfg.planning_rules
    steps = await store.all_steps()

    if len(steps) < rules.min_steps:
        errors.append(
            f"Plan has {len(steps)} steps, "
            f"minimum is {rules.min_steps}"
        )

    if len(steps) > rules.max_steps:
        errors.append(
            f"Plan has {len(steps)} steps, "
            f"maximum is {rules.max_steps}"
        )

    # require_discovery_before: for each step whose type is
    # listed, verify at least one depends_on is discovery_step
    if rules.require_discovery_before:
        step_by_id: dict[str, Step] = {
            s.id: s for s in steps
        }
        for step in steps:
            if step.type.value not in (
                rules.require_discovery_before
            ):
                continue
            has_discovery = any(
                step_by_id.get(dep_id) is not None
                and step_by_id[dep_id].type
                == StepType.DISCOVERY_STEP
                for dep_id in step.depends_on
            )
            if not has_discovery:
                errors.append(
                    f"Step {step.id!r} "
                    f"({step.type.value}) requires "
                    f"a discovery_step dependency"
                )

    return errors


# ---------------------------------------------------------------------------
# Default validation levels per step type
# ---------------------------------------------------------------------------

_DEFAULT_VALIDATION_LEVELS: dict[str, int] = {
    "discovery_step": 1,
    "implement_step": 3,
    "simple_task_step": 2,
    "verify_step": 1,
    "update_docs_step": 2,
}


def _default_validation_level(step_type: str) -> int:
    """Return the default validation level for a step type."""
    cfg = _get_config()
    return cfg.validation.level_by_step_type.get(
        step_type, cfg.validation.default_level,
    )


# ---------------------------------------------------------------------------
# Tool input schemas
# ---------------------------------------------------------------------------

_TASK_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": (
                "Short human-readable label "
                "(max 200 chars)."
            ),
        },
        "description": {
            "type": "string",
            "description": (
                "Full problem statement or issue text."
            ),
        },
        "category": {
            "type": "string",
            "enum": [c.value for c in TaskCategory],
            "description": (
                "Task category. Use bug_fix for SWE-bench "
                "issues, feature for new functionality."
            ),
        },
        "issue_id": {
            "type": "string",
            "description": (
                "Optional tracker reference (e.g. "
                "SWE-bench instance ID)."
            ),
        },
        "acceptance_criteria": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Verifiable conditions for completion."
            ),
        },
        "notes": {
            "type": "string",
            "description": "Free-form planner notes.",
        },
    },
    "required": ["title", "description", "category"],
}

_TASK_GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "Hex UUID of the task.",
        },
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
        "task_id": {
            "type": "string",
            "description": (
                "Hex UUID of the task to update."
            ),
        },
        "title": {
            "type": "string",
            "description": (
                "New title (omit to keep)."
            ),
        },
        "description": {
            "type": "string",
            "description": "New description text.",
        },
        "category": {
            "type": "string",
            "enum": [c.value for c in TaskCategory],
            "description": "New category.",
        },
        "repo_path": {
            "type": "string",
            "description": "New repo path.",
        },
        "issue_id": {
            "type": "string",
            "description": "New issue reference.",
        },
        "acceptance_criteria": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Replacement criteria list."
            ),
        },
        "notes": {
            "type": "string",
            "description": "New notes.",
        },
        "status": {
            "type": "string",
            "enum": [s.value for s in TaskStatus],
            "description": (
                "New status (validates transition)."
            ),
        },
    },
    "required": ["task_id"],
}

_TASK_DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": (
                "Hex UUID of the task to delete."
            ),
        },
    },
    "required": ["task_id"],
}

_ADD_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Add a step to the plan. Choose step_type based on "
        "what the step does: discovery_step for reading/"
        "exploring (tools: Read, Glob, Grep), "
        "implement_step for code changes (tools: Read, Edit, "
        "Write), simple_task_step for shell commands "
        "(tools: Read, Bash), verify_step for checking "
        "results (tools: Read, Grep, Glob), "
        "update_docs_step for docs only (tools: Read, Edit, "
        "Write). validation_level defaults to a sensible "
        "value per step type if omitted."
    ),
    "properties": {
        "step_type": {
            "type": "string",
            "enum": [t.value for t in StepType],
            "description": (
                "Type of step. implement_step for code "
                "changes, discovery_step for exploration, "
                "verify_step for checking, "
                "simple_task_step for shell commands, "
                "update_docs_step for docs."
            ),
        },
        "id": {
            "type": "string",
            "description": (
                "Unique step identifier (e.g. "
                "'fix_regex', 'add_validation')."
            ),
        },
        "goal": {
            "type": "string",
            "description": (
                "Short description of what the step "
                "must accomplish (1-2 sentences)."
            ),
        },
        "briefing": {
            "type": "string",
            "description": (
                "Detailed instructions for the executor "
                "agent. Must be self-contained: include "
                "file paths, line numbers, and exact "
                "code changes. The executor has no "
                "memory of previous steps."
            ),
        },
        "target_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Existing files to modify "
                "(for implement_step, update_docs_step)."
            ),
        },
        "creates_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "New files to create "
                "(for implement_step, update_docs_step)."
            ),
        },
        "deletes_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Files to delete "
                "(for implement_step only)."
            ),
        },
        "function_signatures": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Function signatures to add or modify "
                "(e.g. 'def process_data(items: list[str]) -> dict[str, int]:')."
            ),
        },
        "imports": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "New imports to add "
                "(e.g. 'from typing import Optional')."
            ),
        },
        "classes": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Class names to create or modify "
                "(e.g. 'DataProcessor')."
            ),
        },
        "code_snippets": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Relevant code fragments for context."
            ),
        },
        "depends_on": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Step IDs that must complete first."
            ),
        },
        "success_criterion": {
            "type": "string",
            "description": (
                "Human-readable pass/fail condition "
                "for validation."
            ),
        },
        "validation_level": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "description": (
                "Validation depth. 1=subprocess exit "
                "code, 2-5=LLM with increasing rigor. "
                "Defaults: discovery=1, implement=3, "
                "simple_task=2, verify=1, docs=2."
            ),
        },
        "validator_command": {
            "type": "string",
            "description": (
                "Shell command for level 1 validation."
            ),
        },
        "validator_context": {
            "type": "string",
            "description": (
                "Extra context for levels 2-5 "
                "validation."
            ),
        },
        "intent": {
            "type": "string",
            "enum": [i.value for i in StepIntent],
            "description": "Semantic intent hint.",
        },
    },
    "required": [
        "step_type", "id", "goal", "briefing",
    ],
}



_EDIT_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step_id": {
            "type": "string",
            "description": (
                "ID of the step to edit."
            ),
        },
        "updates": {
            "type": "object",
            "description": (
                "Fields to update on the step."
            ),
            "properties": {
                "goal": {"type": "string"},
                "briefing": {"type": "string"},
                "target_files": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "creates_files": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "deletes_files": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "function_signatures": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Function signatures to add or "
                        "modify (e.g. 'def foo(x: int) "
                        "-> str:')."
                    ),
                },
                "imports": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "New imports to add "
                        "(e.g. 'from typing import Optional')."
                    ),
                },
                "classes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Class names to create or modify "
                        "(e.g. 'DataProcessor')."
                    ),
                },
                "code_snippets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Relevant code fragments for context."
                    ),
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "intent": {
                    "type": "string",
                    "enum": [
                        i.value
                        for i in StepIntent
                    ],
                },
            },
        },
    },
    "required": ["step_id", "updates"],
}

_REMOVE_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step_id": {
            "type": "string",
            "description": (
                "ID of the step to remove."
            ),
        },
    },
    "required": ["step_id"],
}

_INSERT_STEP_AFTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "anchor_id": {
            "type": "string",
            "description": (
                "ID of the anchor step."
            ),
        },
        "step_type": {
            "type": "string",
            "enum": [t.value for t in StepType],
            "description": "Type of the new step.",
        },
        "step_args": {
            "type": "object",
            "description": "Args for the new step.",
            "properties": {
                "id": {"type": "string"},
                "goal": {"type": "string"},
                "briefing": {"type": "string"},
                "target_files": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "creates_files": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "deletes_files": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "function_signatures": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "imports": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "classes": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "code_snippets": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "success_criterion": {
                    "type": "string",
                },
                "validation_level": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                },
                "validator_command": {
                    "type": "string",
                },
                "validator_context": {
                    "type": "string",
                },
                "intent": {"type": "string"},
                "command": {"type": "string"},
            },
            "required": [
                "id", "goal", "briefing",
            ],
        },
    },
    "required": [
        "anchor_id", "step_type",
        "step_args",
    ],
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
            "description": (
                "Planner explanation of the plan."
            ),
        },
    },
    "required": ["rationale"],
}

_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step_id": {
            "type": "string",
            "description": (
                "Step ID to execute. If empty, "
                "auto-picks the next runnable step."
            ),
        },
        "prev_context": {
            "type": "string",
            "description": (
                "Additional context from planner."
            ),
        },
    },
    "required": [],
}

_UPDATE_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": (
                "Context content to inject."
            ),
        },
        "mode": {
            "type": "string",
            "enum": ["replace", "append", "clear"],
            "description": (
                "Update mode: 'replace' overwrites existing context, "
                "'append' adds to the end, 'clear' empties then sets."
            ),
        },
    },
    "required": ["content"],
}

_GET_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
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
    "Create a new task describing the issue to fix.",
    _TASK_CREATE_SCHEMA,
)
async def _task_create_handler(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Handle task_create tool calls."""
    try:
        store = _get_task_store()
        rp = args.get("repo_path") or _get_repo_path()
        desc = TaskDescription(
            title=args["title"],
            description=args["description"],
            category=TaskCategory(args["category"]),
            repo_path=rp,
            issue_id=args.get("issue_id"),
            acceptance_criteria=args.get(
                "acceptance_criteria", [],
            ),
            notes=args.get("notes"),
        )
        task = await store.create(desc)
        return _ok(task.model_dump(mode="json"))
    except (
        RatchetCatalogError,
        RatchetStoreError,
        ValueError,
    ) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error in task_create")
        return _err(exc)


@tool(
    "task_get",
    "Retrieve a task by its ID.",
    _TASK_GET_SCHEMA,
)
async def _task_get_handler(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Handle task_get tool calls."""
    try:
        store = _get_task_store()
        task = await store.get(args["task_id"])
        return _ok(task.model_dump(mode="json"))
    except (
        RatchetCatalogError,
        RatchetStoreError,
    ) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error in task_get")
        return _err(exc)


@tool(
    "task_list",
    "List all tasks sorted by creation time.",
    _TASK_LIST_SCHEMA,
)
async def _task_list_handler(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Handle task_list tool calls."""
    try:
        store = _get_task_store()
        tasks = await store.list_all()
        return _ok([
            t.model_dump(mode="json")
            for t in tasks
        ])
    except (
        RatchetCatalogError,
        RatchetStoreError,
    ) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error in task_list")
        return _err(exc)


@tool(
    "task_update",
    "Update task fields or status.",
    _TASK_UPDATE_SCHEMA,
)
async def _task_update_handler(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Handle task_update tool calls."""
    try:
        store = _get_task_store()
        task_id: str = args["task_id"]
        present = _DESCRIPTION_FIELDS & args.keys()
        new_status: str | None = args.get("status")

        task: Task | None = None

        if present:
            current = await store.get(task_id)
            merged: dict[str, Any] = (
                current.model_dump(
                    include=_DESCRIPTION_FIELDS
                )  # type: ignore[arg-type]
            )
            for field in present:
                merged[field] = (
                    TaskCategory(args[field])
                    if field == "category"
                    else args[field]
                )
            desc = TaskDescription.model_validate(
                merged,
            )
            task = await store.update_description(
                task_id, desc,
            )

        if new_status is not None:
            task = await store.update_status(
                task_id,
                TaskStatus(new_status),
            )

        if task is None:
            task = await store.get(task_id)

        return _ok(task.model_dump(mode="json"))
    except (
        RatchetCatalogError,
        RatchetStoreError,
        ValueError,
    ) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error in task_update")
        return _err(exc)


@tool(
    "task_delete",
    "Delete a task by its ID.",
    _TASK_DELETE_SCHEMA,
)
async def _task_delete_handler(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Handle task_delete tool calls."""
    try:
        store = _get_task_store()
        await store.delete(args["task_id"])
        return _ok({"deleted": args["task_id"]})
    except (
        RatchetCatalogError,
        RatchetStoreError,
    ) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error in task_delete")
        return _err(exc)


# ---------------------------------------------------------------------------
# Plan tool handlers
# ---------------------------------------------------------------------------


def _add_step_handler_impl(
    args: dict[str, Any],
) -> tuple[PlanStore, Step]:
    """Build and validate a step from add_step arguments."""
    store = _get_plan_store()
    step_type_str: str = args["step_type"]
    step_type = StepType(step_type_str)

    # Apply defaults for optional fields
    success_criterion: str = args.get(
        "success_criterion", "",
    )
    if not success_criterion:
        success_criterion = f"Step '{args['id']}' completes successfully"

    validation_level: int = args.get(
        "validation_level", 0,
    ) or _default_validation_level(step_type_str)

    val_spec = ValidatorSpec(
        level=validation_level,
        success_criterion=success_criterion,
        command=args.get("validator_command"),
        extra_context=args.get("validator_context"),
    )

    common: dict[str, Any] = {
        "id": args["id"],
        "type": step_type,
        "goal": args["goal"],
        "briefing": args["briefing"],
        "depends_on": args.get("depends_on", []),
        "validator": val_spec,
    }
    intent_raw: str | None = args.get("intent")
    if intent_raw is not None:
        common["intent"] = StepIntent(intent_raw)

    if step_type in (
        StepType.IMPLEMENT_STEP,
        StepType.UPDATE_DOCS_STEP,
    ):
        common["target_files"] = args.get(
            "target_files", [],
        )
        common["creates_files"] = args.get(
            "creates_files", [],
        )

    if step_type == StepType.IMPLEMENT_STEP:
        common["deletes_files"] = args.get(
            "deletes_files", [],
        )

    # Add implementation detail fields if provided
    impl_fields = [
        "function_signatures", "imports", "classes", "code_snippets",
    ]
    for field in impl_fields:
        if field in args:
            common[field] = args[field]

    step = Step.model_validate(common)
    return store, step


@tool(
    "add_step",
    "Add a step to the plan. Specify step_type to control "
    "which tools the executor can use.",
    _ADD_STEP_SCHEMA,
)
async def _add_step_handler(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Handle add_step tool calls."""
    try:
        store, step = _add_step_handler_impl(args)
        await store.add_step(step)
        return _ok(step.model_dump(mode="json"))
    except (
        RatchetCatalogError,
        RatchetStoreError,
        ValueError,
    ) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error in add_step")
        return _err(exc)


@tool(
    "edit_step",
    "Update fields on a PENDING/FAILED step.",
    _EDIT_STEP_SCHEMA,
)
async def _edit_step_handler(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Handle edit_step tool calls."""
    try:
        store = _get_plan_store()
        step_id: str = args["step_id"]
        updates: dict[str, Any] = args["updates"]

        # Parse nested validator updates
        val_updates: dict[str, Any] = {}
        clean: dict[str, Any] = {}
        for key, value in updates.items():
            if key.startswith("validator_"):
                fmap = {
                    "validator_command": "command",
                    "validator_context": "extra_context",
                }
                if key in fmap:
                    val_updates[fmap[key]] = value
            else:
                clean[key] = value

        # Handle intent enum conversion
        if "intent" in clean and isinstance(
            clean["intent"], str
        ):
            clean["intent"] = StepIntent(
                clean["intent"]
            )

        # Merge validator updates with existing
        if val_updates:
            current = await store.get_step(step_id)
            cv = current.validator.model_dump()
            cv.update(val_updates)
            clean["validator"] = (
                ValidatorSpec.model_validate(cv)
            )

        step = await store.edit_step(step_id, clean)
        return _ok(step.model_dump(mode="json"))
    except (
        RatchetCatalogError,
        RatchetStoreError,
        ValueError,
    ) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error in edit_step")
        return _err(exc)


@tool(
    "remove_step",
    "Delete a PENDING or FAILED step.",
    _REMOVE_STEP_SCHEMA,
)
async def _remove_step_handler(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Handle remove_step tool calls."""
    try:
        store = _get_plan_store()
        sid: str = args["step_id"]
        await store.remove_step(sid)
        return _ok({"removed": sid})
    except (
        RatchetCatalogError,
        RatchetStoreError,
    ) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error in remove_step")
        return _err(exc)


@tool(
    "insert_step_after",
    "Insert a step after the anchor step in the plan.",
    _INSERT_STEP_AFTER_SCHEMA,
)
async def _insert_step_after_handler(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Handle insert_step_after tool calls."""
    try:
        anchor_id: str = args["anchor_id"]
        stype: str = args["step_type"]
        step_args: dict[str, Any] = args["step_args"]
        full_args = {
            "step_type": stype, **step_args,
        }
        store, step = _add_step_handler_impl(full_args)
        await store.insert_step_after(anchor_id, step)
        return _ok(step.model_dump(mode="json"))
    except (
        RatchetCatalogError,
        RatchetStoreError,
        ValueError,
    ) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error in insert_step_after")
        return _err(exc)


@tool(
    "view_plan",
    "Return the current plan state.",
    _VIEW_PLAN_SCHEMA,
)
async def _view_plan_handler(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Handle view_plan tool calls."""
    try:
        store = _get_plan_store()
        snapshot = await store.view()
        return _ok(snapshot)
    except (
        RatchetCatalogError,
        RatchetStoreError,
    ) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error in view_plan")
        return _err(exc)


@tool(
    "submit_plan",
    "Mark the plan as submitted.",
    _SUBMIT_PLAN_SCHEMA,
)
async def _submit_plan_handler(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Handle submit_plan tool calls."""
    try:
        store = _get_plan_store()
        cfg = _get_config()
        rationale: str = args["rationale"]

        # Validate planning rules before accepting
        rule_errors = await _validate_plan_rules(
            store, cfg,
        )
        if rule_errors:
            return _err(
                RatchetCatalogError(
                    "Plan validation failed:\n"
                    + "\n".join(
                        f"- {e}" for e in rule_errors
                    )
                )
            )

        # Run refiner agent if enabled
        if cfg.refiner.enabled:
            from ratchet.exec.refiner import refine

            rp = _get_repo_path()
            steps = await store.all_steps()
            verdict = await refine(
                steps, rationale, cfg, rp,
            )
            if not verdict.approved:
                recs = "\n".join(
                    f"- {r}"
                    for r in verdict.recommendations
                )
                return _err(
                    RatchetCatalogError(
                        f"Plan rejected by refiner "
                        f"(score={verdict.score}): "
                        f"{verdict.diagnosis}\n"
                        f"Recommendations:\n{recs}"
                    )
                )

        await store.submit(rationale)
        return _ok({
            "submitted": True,
            "rationale": rationale,
        })
    except (
        RatchetCatalogError,
        RatchetStoreError,
    ) as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error in submit_plan")
        return _err(exc)


# ---------------------------------------------------------------------------
# Step execution tool
# ---------------------------------------------------------------------------


@tool(
    "step",
    "Execute the next runnable step, or a "
    "specific step_id. Returns result with "
    "error_type: null or 'sdk_error'. If "
    "'sdk_error', failure is infrastructure "
    "-- do NOT create fix-up steps.",
    _STEP_SCHEMA,
)
async def _step_handler(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Handle step execution tool calls.

    This is a thin wrapper around run_step().
    All actual logic lives in plan_executor.run_step().
    """
    try:
        # Get dependencies from ContextVars
        store = _get_plan_store()
        state = _get_state()
        cfg = _get_config()
        rp = _get_repo_path()

        # Guard: plan must be submitted before execution
        if not store.is_submitted:
            return _err(RatchetCatalogError(
                "Plan has not been submitted yet. "
                "Call submit_plan before executing "
                "steps."
            ))

        # Extract args
        step_id = args.get("step_id")
        prev_context = args.get("prev_context", "")

        # Lazy import to avoid circular dependency
        from ratchet.exec.plan_executor import run_step

        # Delegate to single source of truth
        result = await run_step(
            step_id=step_id,
            prev_context=prev_context,
            plan_store=store,
            state=state,
            cfg=cfg,
            repo_path=rp,
            restrictions=cfg.restrictions,
        )

        return _ok(result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error in step tool")
        return _err(exc)


@tool(
    "update_context",
    "Update the dynamic planner context that persists across turns. "
    "Use this to inject information discovered during execution "
    "that the planner should know about in future turns.",
    _UPDATE_CONTEXT_SCHEMA,
)
async def _update_context_handler(**kwargs: Any) -> dict[str, Any]:
    """Handle update_context tool calls."""
    try:
        store = _get_planner_context()
        content = kwargs.get("content", "")
        mode = kwargs.get("mode", "replace")
        updated = store.set(content, mode)
        return _ok({
            "status": "updated",
            "context": updated,
            "mode": mode,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error in update_context")
        return _err(exc)


@tool(
    "get_context",
    "Retrieve the current dynamic planner context.",
    _GET_CONTEXT_SCHEMA,
)
async def _get_context_handler(**kwargs: Any) -> dict[str, Any]:
    """Handle get_context tool calls."""
    try:
        store = _get_planner_context()
        current = store.get()
        return _ok({
            "context": current,
            "empty": len(current.strip()) == 0,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error in get_context")
        return _err(exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

TASK_TOOL_NAMES: list[str] = [
    "task_create",
    "task_get",
    "task_list",
    "task_update",
    "task_delete",
]

PLAN_TOOL_NAMES: list[str] = [
    "add_step",
    "edit_step",
    "remove_step",
    "insert_step_after",
    "view_plan",
    "submit_plan",
]

CONTEXT_TOOL_NAMES: list[str] = [
    "update_context",
    "get_context",
]

STEP_TOOL_NAME: str = "step"

ALL_TOOL_NAMES: list[str] = (
    TASK_TOOL_NAMES
    + PLAN_TOOL_NAMES
    + CONTEXT_TOOL_NAMES
    + [STEP_TOOL_NAME]
)

_ALL_HANDLERS = [
    # Task tools
    _task_create_handler,
    _task_get_handler,
    _task_list_handler,
    _task_update_handler,
    _task_delete_handler,
    # Plan tools
    _add_step_handler,
    _edit_step_handler,
    _remove_step_handler,
    _insert_step_after_handler,
    _view_plan_handler,
    _submit_plan_handler,
    # Context tools
    _update_context_handler,
    _get_context_handler,
    # Step execution
    _step_handler,
]


def build_catalog_server() -> McpSdkServerConfig:
    """Build the ratchet_catalog MCP server.

    Registers all Task, Plan, and step execution tools.
    Call bind_catalog_context() before starting
    a planner session.

    Returns:
        McpSdkServerConfig for use in
        ClaudeAgentOptions.mcp_servers.
    """
    return create_sdk_mcp_server(
        "ratchet_catalog", tools=_ALL_HANDLERS,
    )


def bind_catalog_context(
    task_store: TaskStore,
    plan_store: PlanStore,
    state: State,
    repo_path: str,
    cfg: Config,
) -> None:
    """Bind ContextVars for catalog tool handlers.

    Must be called by the orchestrator before the
    planner session starts.

    Args:
        task_store: TaskStore for this session.
        plan_store: PlanStore for this session.
        state: State for recording step outputs.
        repo_path: Absolute path to the repo.
        cfg: Config for this solve session.
    """
    _task_store.set(task_store)
    _plan_store.set(plan_store)
    _state.set(state)
    _repo_path.set(repo_path)
    _config.set(cfg)
    _planner_context.set(PlannerContextStore())
