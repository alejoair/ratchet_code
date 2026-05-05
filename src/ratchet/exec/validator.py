"""Step validator for the ratchet pipeline.

Level 1 uses subprocess.run; levels 2-5 use sandboxed SDK clients
with read-only tools and structured ValidationVerdict output
via the SDK's output_format mechanism.
"""

import logging
import os
import subprocess

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    query,
)

from ratchet.config import Config
from ratchet.plan.schema import (
    Step,
    StepResult,
    ValidationVerdict,
    sdk_output_schema,
)

logger = logging.getLogger(__name__)

# The parent CLI sets CLAUDECODE=1 which causes nested SDK sessions
# to fail with exit code 1. We must strip it before spawning.
# See: https://github.com/anthropics/claude-agent-sdk-python/issues/573
_CLEAN_ENV = {
    k: v for k, v in os.environ.items()
    if k != "CLAUDECODE"
}

VALIDATOR_TOOLS_BY_LEVEL: dict[int, list[str]] = {
    2: ["Read", "Bash"],
    3: ["Read", "Bash", "Grep", "Glob"],
    4: ["Read", "Bash", "Grep", "Glob"],
    5: ["Read", "Bash", "Grep", "Glob"],
}


def _validate_level_1(
    step: Step,
    result: StepResult,
    repo_path: str,
) -> ValidationVerdict:
    """Run a shell command and check exit code.

    If no command is provided, check the executor's success
    status before deciding whether to pass or fail.
    Level 1 without a command means "verify the executor
    succeeded".
    """
    command = step.validator.command
    if not command:
        if result.success:
            return ValidationVerdict(
                passed=True,
                diagnosis=(
                    "No validation command provided; "
                    "auto-passing based on executor "
                    "success"
                ),
            )
        else:
            return ValidationVerdict(
                passed=False,
                diagnosis=(
                    "No validation command provided "
                    "but executor failed; marking as "
                    f"failed. Error: {result.error}"
                ),
            )

    proc_result = subprocess.run(
        command,
        shell=True,
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    stdout_tail = (
        proc_result.stdout[-500:]
        if proc_result.stdout
        else ""
    )
    stderr_tail = (
        proc_result.stderr[-500:]
        if proc_result.stderr
        else ""
    )

    if proc_result.returncode == 0:
        return ValidationVerdict(
            passed=True,
            diagnosis=(
                f"Command exited 0. "
                f"stdout: {stdout_tail}"
            ),
        )

    return ValidationVerdict(
        passed=False,
        diagnosis=(
            f"Command exited "
            f"{proc_result.returncode}. "
            f"stdout: {stdout_tail} "
            f"stderr: {stderr_tail}"
        ),
    )


def _render_validator_prompt(
    step: Step,
    result: StepResult,
    level: int,
) -> str:
    """Render the user prompt for a validator session."""
    level_descriptions: dict[int, str] = {
        2: (
            "Run the validation command and "
            "interpret output"
        ),
        3: (
            "Functionally verify the "
            "implementation by inspecting "
            "files and running checks"
        ),
        4: (
            "Semantically verify the "
            "implementation matches the "
            "intent, not just exit codes"
        ),
        5: (
            "Adversarially search for "
            "regressions, edge cases, and "
            "broken invariants"
        ),
    }

    lines: list[str] = [
        f"Validation Level: {level}",
        f"Approach: "
        f"{level_descriptions.get(level, '')}",
        "",
        f"Step goal: {step.goal}",
        f"Success criterion: "
        f"{step.validator.success_criterion}",
    ]

    if result.output is not None:
        lines.append(
            f"Executor summary: "
            f"{result.output.summary}"
        )
        if result.output.notes:
            lines.append(
                f"Executor notes: "
                f"{result.output.notes}"
            )
    elif result.error:
        lines.append(
            f"Executor error: {result.error}"
        )

    if step.target_files:
        lines.append(
            "Modified files: "
            + ", ".join(step.target_files)
        )
    if step.creates_files:
        lines.append(
            "Created files: "
            + ", ".join(step.creates_files)
        )

    if step.validator.extra_context:
        lines.append(
            f"Additional context: "
            f"{step.validator.extra_context}"
        )

    return "\n".join(lines)


async def _validate_level_2_to_5(
    step: Step,
    result: StepResult,
    cfg: Config,
    repo_path: str,
    level: int,
) -> ValidationVerdict:
    """Run an LLM-based validation session."""
    model = cfg.validator_model_for(level)
    if not model:
        return ValidationVerdict(
            passed=False,
            diagnosis=(
                f"No model configured for "
                f"validator level {level}"
            ),
        )

    tools = VALIDATOR_TOOLS_BY_LEVEL.get(
        level, ["Read"],
    )
    prompt = _render_validator_prompt(
        step, result, level,
    )

    validator_prompt = (
        "You are a code validation agent. "
        "Your job is to verify whether the "
        "implementation meets the success "
        "criterion.\n\n"
        "RULES:\n"
        "- Do NOT edit or write any files.\n"
        "- Only use your tools to read files "
        "and run read-only commands.\n"
        "- Focus solely on whether the "
        "success criterion is met."
    )

    def _on_stderr(line: str) -> None:
        logger.warning(
            "Validator stderr [%s]: %s",
            step.id,
            line,
        )

    output_schema = sdk_output_schema(ValidationVerdict)

    options = ClaudeAgentOptions(
        model=model,
        cwd=repo_path,
        system_prompt=validator_prompt,
        tools=tools,
        allowed_tools=tools,
        max_turns=cfg.max_validator_turns(level),
        permission_mode="acceptEdits",
        env=_CLEAN_ENV,
        stderr=_on_stderr,
        output_format={
            "type": "json_schema",
            "schema": output_schema,
        },
    )

    last_msg: ResultMessage | None = None
    real_model: str | None = None
    try:
        async for msg in query(
            prompt=prompt, options=options,
        ):
            if isinstance(msg, ResultMessage):
                last_msg = msg
            elif isinstance(msg, AssistantMessage):
                if msg.model and not real_model:
                    real_model = msg.model
                    logger.info(
                        "Validator [%s] using "
                        "actual model=%s "
                        "(requested=%s)",
                        step.id,
                        real_model,
                        model,
                    )
            else:
                logger.debug(
                    "Validator msg type=%s: %s",
                    type(msg).__name__,
                    str(msg)[:200],
                )
    except Exception as exc:  # noqa: BLE001
        if last_msg is not None:
            logger.warning(
                "Validator SDK raised after "
                "delivering ResultMessage for "
                "step %s (subtype=%s, "
                "real_model=%s): %s",
                step.id,
                last_msg.subtype,
                real_model or model,
                exc,
            )
        else:
            logger.exception(
                "Validator session failed for "
                "step %s (model=%s, tools=%s, "
                "max_turns=%d)",
                step.id,
                model,
                tools,
                cfg.max_validator_turns(level),
            )
            return ValidationVerdict(
                passed=False,
                diagnosis=(
                    "Validator SDK session "
                    f"failed: {exc}"
                ),
            )

    if last_msg is None:
        return ValidationVerdict(
            passed=False,
            diagnosis=(
                "No ResultMessage from "
                "validator SDK"
            ),
        )

    # Read structured output from SDK
    raw = last_msg.structured_output
    if raw is not None:
        try:
            return ValidationVerdict.model_validate(
                raw,
            )
        except ValueError:
            logger.warning(
                "Validator structured_output "
                "failed validation for step %s",
                step.id,
            )

    # Fallback: wrap result text as diagnosis
    text = last_msg.result or ""
    if text.strip():
        return ValidationVerdict(
            passed=False,
            diagnosis=text.strip()[:500],
        )

    return ValidationVerdict(
        passed=False,
        diagnosis=(
            "Validator produced no structured "
            "output"
        ),
    )


async def validate(
    step: Step,
    result: StepResult,
    cfg: Config,
    repo_path: str,
) -> ValidationVerdict:
    """Validate an executed step.

    Args:
        step: The step that was executed.
        result: The executor's StepResult.
        cfg: Active configuration.
        repo_path: Absolute path to the target repo.

    Returns:
        A ValidationVerdict indicating pass/fail.
    """
    level = step.validator.level
    logger.info(
        "Validating step %s at level %d",
        step.id, level,
    )

    if level == 1:
        return _validate_level_1(
            step, result, repo_path,
        )

    return await _validate_level_2_to_5(
        step, result, cfg, repo_path, level,
    )
