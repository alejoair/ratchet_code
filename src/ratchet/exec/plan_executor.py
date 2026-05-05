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
from ratchet.plan.catalog import check_prerequisites
from ratchet.plan.schema import StepOutput
from ratchet.plan.store import PlanStore
from ratchet.state import State

logger = logging.getLogger(__name__)


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

    If the step is FAILED, it is automatically reset to PENDING
    so it can be re-executed without requiring edit_step first.

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

    # Auto-reset FAILED steps so they can be re-executed
    status = await plan_store.get_status(step_id)
    if status.value == "failed":
        # edit_step with empty updates resets
        # FAILED -> PENDING per store rules
        await plan_store.edit_step(
            step_id, {},
        )
        logger.info(
            "Auto-reset step %s from "
            "FAILED to PENDING for retry",
            step_id,
        )
        # Refresh step after edit
        step = await plan_store.get_step(step_id)

    # Check prerequisites
    prereq_errors = await check_prerequisites(
        step, plan_store, repo_path, cfg,
    )
    if prereq_errors:
        return {
            "status": "prerequisites_not_met",
            "step_id": step_id,
            "errors": prereq_errors,
        }

    # Mark in progress
    await plan_store.mark_in_progress(step_id)

    # Build full context from dependencies and planner input
    deps = state.resolve(step.depends_on)
    auto_ctx_parts: list[str] = [
        f"[{sid}] {out.summary}"
        for sid, out in deps.items()
    ]
    auto_ctx = '\n'.join(auto_ctx_parts)

    # Run context builder if enabled and step type matches
    enriched = ""
    builder_cfg = cfg.context_builder
    if builder_cfg.enabled:
        run_before = builder_cfg.run_before
        should_run = (
            not run_before
            or step.type.value in run_before
        )
        if should_run:
            from ratchet.exec.context_builder import (
                build_context,
            )

            ctx_result = await build_context(
                step, cfg, repo_path, deps,
            )
            if ctx_result.enriched_context:
                enriched = (
                    ctx_result.enriched_context
                )

    # Assemble full context: deps + enriched + planner
    ctx_parts: list[str] = []
    if auto_ctx:
        ctx_parts.append(auto_ctx)
    if enriched:
        ctx_parts.append(enriched)
    if prev_context:
        ctx_parts.append(prev_context)
    full_context = "\n\n".join(ctx_parts)

    # Execute
    result = await execute_step(
        step=step,
        cfg=cfg,
        repo_path=repo_path,
        state=state,
        restrictions=restrictions,
        prev_context=full_context,
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
        "error_type": getattr(result, "error_type", None),
        "output": (
            result.output.model_dump(mode="json")
            if result.output else None
        ),
        "verdict": verdict.model_dump(mode="json"),
    }
