"""Step tool body for the ratchet pipeline.

Implements the logic that runs when the planner calls the ``step`` tool:
resolve step, check prerequisites, execute, validate, update stores,
and return the verdict JSON.
"""

import logging
from typing import Any

from ratchet.config import Config
from ratchet.exec.executor import execute_step
from ratchet.exec.validator import validate
from ratchet.plan.schema import StepOutput
from ratchet.plan.store import PlanStore, StepStatus
from ratchet.state import State

logger = logging.getLogger(__name__)


async def _check_deps(
    step_id: str,
    plan_store: PlanStore,
) -> list[str] | None:
    """Check prerequisites for a step.

    Args:
        step_id: The step to check.
        plan_store: The active plan store.

    Returns:
        None if all met, list of unmet dep IDs otherwise.
    """
    step = await plan_store.get_step(step_id)
    unmet: list[str] = []
    for dep_id in step.depends_on:
        status = await plan_store.get_status(dep_id)
        if status != StepStatus.COMPLETED:
            unmet.append(dep_id)
    return unmet if unmet else None


async def run_step(
    step_id: str | None,
    prev_context: str,
    plan_store: PlanStore,
    state: State,
    cfg: Config,
    repo_path: str,
    restrictions: str,
) -> dict[str, Any]:
    """Execute a single step from the plan.

    This is the body of the ``step`` catalog tool. It resolves the
    step (or picks the next runnable one), checks prerequisites,
    runs the executor and validator, updates the plan store and
    state, and returns the verdict JSON to the planner.

    Args:
        step_id: Explicit step ID, or None to auto-pick.
        prev_context: Extra context from the planner.
        plan_store: The active plan store.
        state: Shared state for step outputs.
        cfg: Active configuration.
        repo_path: Absolute path to the target repository.
        restrictions: Project restrictions from CLAUDE.md.

    Returns:
        Dict with the step result and validation verdict.
    """
    # Resolve step_id
    if step_id is None:
        step_id = await plan_store.next_runnable_id()
    if step_id is None:
        return {
            "status": "no_runnable_step",
            "message": "No runnable step available.",
        }

    step = await plan_store.get_step(step_id)

    # Check prerequisites
    unmet = await _check_deps(step_id, plan_store)
    if unmet is not None:
        return {
            "status": "prerequisites_not_met",
            "step_id": step_id,
            "unmet_deps": unmet,
        }

    # Mark in progress
    await plan_store.mark_in_progress(step_id)

    # Execute
    result = await execute_step(
        step=step,
        cfg=cfg,
        repo_path=repo_path,
        state=state,
        restrictions=restrictions,
        prev_context=prev_context,
    )

    # Validate
    verdict = await validate(
        step=step,
        result=result,
        cfg=cfg,
        repo_path=repo_path,
    )

    # Update stores based on outcome
    if result.success and verdict.passed:
        output = result.output or StepOutput(
            summary="Step completed",
        )
        await plan_store.mark_completed(
            step_id, output, verdict,
        )
        state.record(step_id, output)
    else:
        await plan_store.mark_failed(step_id, verdict)

    logger.info(
        "Step %s: success=%s verdict=%s",
        step_id,
        result.success,
        verdict.passed,
    )

    return {
        "status": (
            "completed" if verdict.passed else "failed"
        ),
        "step_id": step_id,
        "executor_success": result.success,
        "error": result.error,
        "verdict": verdict.model_dump(mode="json"),
    }
