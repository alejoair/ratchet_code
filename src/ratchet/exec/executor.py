"""Step executor for the ratchet pipeline.

Builds a sandboxed ClaudeAgentOptions per step type, renders the step
briefing as the user prompt, runs the SDK query, and captures the
structured StepOutput via the SDK's output_format mechanism.
"""

import logging
import os
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    query,
)

from ratchet.config import Config
from ratchet.plan.schema import (
    Step,
    StepOutput,
    StepResult,
    StepType,
    sdk_output_schema,
)
from ratchet.state import State

logger = logging.getLogger(__name__)

# The parent CLI sets CLAUDECODE=1 which causes nested SDK sessions
# to fail with exit code 1. We must strip it before spawning.
_CLEAN_ENV = {
    k: v for k, v in os.environ.items()
    if k != "CLAUDECODE"
}

# ---------------------------------------------------------------------------
# Tool sandbox per step type
# ---------------------------------------------------------------------------

TOOLS_BY_STEP_TYPE: dict[StepType, list[str]] = {
    StepType.DISCOVERY_STEP: ["Read", "Glob", "Grep"],
    StepType.IMPLEMENT_STEP: ["Read", "Edit", "Write"],
    StepType.SIMPLE_TASK_STEP: ["Read", "Bash"],
    StepType.VERIFY_STEP: ["Read", "Grep", "Glob"],
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
      2. Additional context from planner (if non-empty).
      3. Goal.
      4. File declarations.
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
        lines: list[str] = [
            "Context from previous steps:",
        ]
        for sid, out in deps.items():
            lines.append(
                f"  [{sid}] {out.summary}"
            )
            if out.notes:
                lines.append(
                    f"    Notes: {out.notes}"
                )
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
            "Create files: "
            + ", ".join(step.creates_files)
        )
    if step.deletes_files:
        blocks.append(
            "Delete files: "
            + ", ".join(step.deletes_files)
        )

    blocks.append(
        f"Success criterion: "
        f"{step.validator.success_criterion}"
    )

    return "\n\n".join(blocks)


def _extract_step_output(
    msg: ResultMessage,
) -> StepOutput | None:
    """Extract StepOutput from SDK structured_output.

    The SDK may wrap the output as {"output": {...}}.
    Unwrap if the top-level dict has exactly one key
    "output".

    Args:
        msg: The final ResultMessage from the SDK.

    Returns:
        A parsed StepOutput, or None if not found.
    """
    raw: Any = msg.structured_output
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
        try:
            return StepOutput.model_validate(data)
        except ValueError:
            return None
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
        repo_path: Absolute path to the target repo.
        state: Shared state for resolving prerequisites.
        restrictions: Project restrictions from CLAUDE.md.
        prev_context: Extra context from the planner.

    Returns:
        A StepResult with output on success or error.
    """
    deps = state.resolve(step.depends_on)
    prompt = render_step_prompt(
        step, deps, prev_context,
    )

    system_body = (
        "You are a code modification agent. "
        "Your job is to make the exact changes "
        "described below. You MUST follow these "
        "rules strictly:\n\n"
        "RULES:\n"
        "- Only modify files listed as target "
        "files. Do NOT touch any other files.\n"
        "- Do NOT create test files, "
        "verification scripts, or docs.\n"
        "- Do NOT delete any existing files.\n"
        "- Do NOT add comments explaining what "
        "you changed unless specifically asked.\n"
        "- Make the minimal, surgical change "
        "needed. Do not refactor surrounding "
        "code.\n\n"
        f"TASK BRIEFING:\n{step.briefing}\n"
    )

    if restrictions:
        system_body += (
            f"\nProject restrictions:\n"
            f"{restrictions}\n"
        )

    tools = TOOLS_BY_STEP_TYPE.get(
        step.type, ["Read"],
    )

    def _on_stderr(line: str) -> None:
        logger.warning(
            "Executor stderr [%s]: %s",
            step.id, line,
        )

    output_schema = sdk_output_schema(StepOutput)

    options = ClaudeAgentOptions(
        model=cfg.executor_model_for(step.type),
        cwd=repo_path,
        system_prompt=system_body,
        tools=tools,
        allowed_tools=tools,
        max_turns=cfg.budgets.max_executor_turns,
        permission_mode="acceptEdits",
        env=_CLEAN_ENV,
        stderr=_on_stderr,
        output_format={
            "type": "json_schema",
            "schema": output_schema,
        },
    )

    last_result: ResultMessage | None = None
    try:
        async for msg in query(
            prompt=prompt, options=options,
        ):
            if isinstance(msg, ResultMessage):
                last_result = msg
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Executor session failed for step %s",
            step.id,
        )
        return StepResult(
            step_id=step.id,
            output=None,
            success=False,
            error=str(exc),
            error_type="sdk_error",
        )

    if last_result is None:
        return StepResult(
            step_id=step.id,
            output=None,
            success=False,
            error=(
                "No ResultMessage received from SDK"
            ),
            error_type="sdk_error",
        )

    subtype = getattr(last_result, "subtype", None)
    if subtype != "success":
        return StepResult(
            step_id=step.id,
            output=None,
            success=False,
            error=(
                f"SDK session ended with "
                f"subtype: {subtype}"
            ),
            error_type="sdk_error",
        )

    output = _extract_step_output(last_result)

    if output is None:
        # Last resort: wrap result text as summary
        text = last_result.result or ""
        if text.strip():
            output = StepOutput(
                summary=text.strip()[:500],
            )
        else:
            return StepResult(
                step_id=step.id,
                output=None,
                success=False,
                error=(
                    "Executor completed but "
                    "produced no output"
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
