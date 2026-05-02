"""Step executor for the ratchet pipeline.

Builds a sandboxed ClaudeAgentOptions per step type, renders the step
briefing as the user prompt, runs the SDK query, and captures the
structured StepOutput.
"""

import logging
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from ratchet.config import Config
from ratchet.plan.schema import Step, StepOutput, StepResult, StepType
from ratchet.state import State

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool sandbox per step type
# ---------------------------------------------------------------------------

TOOLS_BY_STEP_TYPE: dict[StepType, list[str]] = {
    StepType.DISCOVERY_STEP: ["Read", "Glob", "Grep"],
    StepType.IMPLEMENT_STEP: ["Read", "Edit", "Write"],
    StepType.SIMPLE_TASK_STEP: ["Read", "Bash"],
    StepType.VERIFY_STEP: ["Read", "Bash"],
    StepType.UPDATE_DOCS_STEP: ["Read", "Edit", "Write"],
}

# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def render_step_prompt(
    step: Step,
    deps: dict[str, StepOutput],
    prev_context: str,
) -> str:
    """Render the user message for an executor session.

    Blocks in order:
      1. Context from previous steps (if deps non-empty).
      2. Additional context from planner (if prev_context non-empty).
      3. Goal.
      4. File declarations (target_files, creates_files, deletes_files).
      5. Success criterion.

    Args:
        step: The step to render.
        deps: Outputs from prerequisite steps.
        prev_context: Extra context from the planner.

    Returns:
        The rendered prompt string.
    """
    blocks: list[str] = []

    if deps:
        lines: list[str] = ["Context from previous steps:"]
        for sid, out in deps.items():
            lines.append(f"  [{sid}] {out.summary}")
            if out.notes:
                lines.append(f"    Notes: {out.notes}")
            if out.artifacts:
                for k, v in out.artifacts.items():
                    lines.append(f"    {k}: {v}")
        blocks.append("\n".join(lines))

    if prev_context:
        blocks.append(
            f"Additional context from planner:\n"
            f"{prev_context}"
        )

    blocks.append(f"Goal: {step.goal}")

    if step.target_files:
        blocks.append(
            "Target files (modify): "
            + ", ".join(step.target_files)
        )
    if step.creates_files:
        blocks.append(
            "Create files: " + ", ".join(step.creates_files)
        )
    if step.deletes_files:
        blocks.append(
            "Delete files: " + ", ".join(step.deletes_files)
        )

    blocks.append(
        f"Success criterion: "
        f"{step.validator.success_criterion}"
    )

    return "\n\n".join(blocks)


def _unwrap_structured_output(
    raw: Any,
) -> StepOutput | None:
    """Parse StepOutput from SDK structured_output.

    The SDK may wrap the output as {"output": {...}}.
    Unwrap if the top-level dict has exactly one key "output".

    Args:
        raw: The structured_output value from ResultMessage.

    Returns:
        A parsed StepOutput, or None if parsing fails.
    """
    if raw is None:
        return None
    data: Any = raw
    if (
        isinstance(data, dict)
        and len(data) == 1
        and "output" in data
    ):
        data = data["output"]
    if isinstance(data, dict):
        return StepOutput.model_validate(data)
    if isinstance(data, str):
        return StepOutput.model_validate_json(data)
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def execute_step(
    step: Step,
    cfg: Config,
    repo_path: str,
    state: State,
    restrictions: str,
    prev_context: str = "",
) -> StepResult:
    """Execute a single plan step via a sandboxed SDK session.

    Args:
        step: The step to execute.
        cfg: Active configuration.
        repo_path: Absolute path to the target repository.
        state: Shared state for resolving prerequisite outputs.
        restrictions: Project restrictions from CLAUDE.md.
        prev_context: Extra context from the planner.

    Returns:
        A StepResult with output on success or error on failure.
    """
    deps = state.resolve(step.depends_on)
    prompt = render_step_prompt(step, deps, prev_context)

    system_append = f"{step.briefing}\n\n"
    if restrictions:
        system_append += (
            f"Project restrictions:\n{restrictions}"
        )

    tools = TOOLS_BY_STEP_TYPE.get(
        step.type, ["Read"],
    )

    options = ClaudeAgentOptions(
        model=cfg.executor_model_for(step.type),
        cwd=repo_path,
        setting_sources=["project"],
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": system_append,
        },
        tools=tools,
        allowed_tools=tools,
        output_format={
            "type": "json_schema",
            "json_schema": StepOutput.model_json_schema(),
        },
        max_turns=cfg.budgets.max_executor_turns,
        permission_mode="acceptEdits",
    )

    last_result: ResultMessage | None = None
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, ResultMessage):
                last_result = msg
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Executor session failed for step %s", step.id,
        )
        return StepResult(
            step_id=step.id,
            output=None,
            success=False,
            error=str(exc),
        )

    if last_result is None:
        return StepResult(
            step_id=step.id,
            output=None,
            success=False,
            error="No ResultMessage received from SDK",
        )

    subtype = getattr(last_result, "subtype", None)
    if subtype != "success":
        return StepResult(
            step_id=step.id,
            output=None,
            success=False,
            error=f"SDK session ended with subtype: {subtype}",
        )

    structured = getattr(
        last_result, "structured_output", None,
    )
    output = _unwrap_structured_output(structured)

    if output is None:
        return StepResult(
            step_id=step.id,
            output=None,
            success=False,
            error=(
                "Executor completed but produced no "
                "structured output"
            ),
        )

    logger.info(
        "Executor step %s completed: %s",
        step.id,
        output.summary[:80],
    )
    return StepResult(
        step_id=step.id,
        output=output,
        success=True,
        error=None,
    )
