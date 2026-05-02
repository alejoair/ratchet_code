"""Step validator for the ratchet pipeline.

Level 1 uses subprocess.run; levels 2-5 use sandboxed SDK clients
with read-only tools and structured ValidationVerdict output.
"""

import json
import logging
import re
import subprocess

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from ratchet.config import Config
from ratchet.plan.schema import (
    Step,
    StepResult,
    ValidationVerdict,
)

logger = logging.getLogger(__name__)

VALIDATOR_TOOLS_BY_LEVEL: dict[int, list[str]] = {
    2: ["Read", "Bash"],
    3: ["Read", "Bash", "Grep", "Glob"],
    4: ["Read", "Bash", "Grep", "Glob"],
    5: ["Read", "Bash", "Grep", "Glob"],
}


def _validate_level_1(
    step: Step,
    repo_path: str,
) -> ValidationVerdict:
    """Run a shell command and check exit code."""
    command = step.validator.command
    if not command:
        return ValidationVerdict(
            passed=False,
            diagnosis=(
                "Level 1 validation requires a command "
                "but none was provided"
            ),
        )

    result = subprocess.run(
        command,
        shell=True,
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    stdout_tail = result.stdout[-500:] if result.stdout else ""
    stderr_tail = result.stderr[-500:] if result.stderr else ""

    if result.returncode == 0:
        return ValidationVerdict(
            passed=True,
            diagnosis=f"Command exited 0. stdout: {stdout_tail}",
        )

    return ValidationVerdict(
        passed=False,
        diagnosis=(
            f"Command exited {result.returncode}. "
            f"stdout: {stdout_tail} stderr: {stderr_tail}"
        ),
    )


def _render_validator_prompt(
    step: Step,
    result: StepResult,
    level: int,
) -> str:
    """Render the user prompt for a validator session."""
    level_descriptions: dict[int, str] = {
        2: "Run the validation command and interpret output",
        3: (
            "Functionally verify the implementation by "
            "inspecting files and running checks"
        ),
        4: (
            "Semantically verify the implementation matches "
            "the intent, not just exit codes"
        ),
        5: (
            "Adversarially search for regressions, edge "
            "cases, and broken invariants"
        ),
    }

    lines: list[str] = [
        f"Validation Level: {level}",
        f"Approach: {level_descriptions.get(level, '')}",
        "",
        f"Step goal: {step.goal}",
        f"Success criterion: {step.validator.success_criterion}",
    ]

    if result.output is not None:
        lines.append(f"Executor summary: {result.output.summary}")
        if result.output.notes:
            lines.append(f"Executor notes: {result.output.notes}")
    elif result.error:
        lines.append(f"Executor error: {result.error}")

    if step.target_files:
        lines.append("Modified files: " + ", ".join(step.target_files))
    if step.creates_files:
        lines.append("Created files: " + ", ".join(step.creates_files))

    if step.validator.extra_context:
        lines.append(f"Additional context: {step.validator.extra_context}")

    return "\n".join(lines)


def _parse_verdict(
    msg: ResultMessage,
) -> ValidationVerdict | None:
    """Parse ValidationVerdict from a ResultMessage text response.

    The validator prompts the model to include a JSON block
    wrapped in ```json ... ``` at the end of its response.
    This function extracts and parses it.

    Args:
        msg: The final ResultMessage from the SDK.

    Returns:
        A parsed ValidationVerdict, or None if no valid JSON found.
    """
    text = msg.result or ""
    # Try to find a ```json ... ``` block
    match = re.search(
        r"```json\s*(.*?)\s*```", text, re.DOTALL,
    )
    if match:
        try:
            data = json.loads(match.group(1))
            return ValidationVerdict.model_validate(data)
        except (json.JSONDecodeError, ValueError):
            pass
    # Fallback: try to parse the entire result as JSON
    try:
        data = json.loads(text)
        return ValidationVerdict.model_validate(data)
    except (json.JSONDecodeError, ValueError):
        pass
    return None


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
            diagnosis=f"No model configured for validator level {level}",
        )

    tools = VALIDATOR_TOOLS_BY_LEVEL.get(level, ["Read"])
    prompt = _render_validator_prompt(step, result, level)

    schema_hint = (
        "You are a code validation agent. Your job is to "
        "verify whether the implementation meets the "
        "success criterion.\n\n"
        "Do NOT edit or write any files. You are read-only.\n\n"
        "IMPORTANT: You MUST end your response with a JSON "
        "block wrapped in ```json ... ``` containing your "
        "verdict in this exact schema:\n"
        '{"passed": true/false, '
        '"diagnosis": "<explanation>", '
        '"suggested_fixes": ["<fix1>"]}\n'
        "This JSON block is mandatory."
    )

    options = ClaudeAgentOptions(
        model=model,
        cwd=repo_path,
        setting_sources=["project"],
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": schema_hint,
        },
        tools=tools,
        allowed_tools=tools,
        disallowed_tools=["Edit", "Write"],
        max_turns=cfg.max_validator_turns(level),
        permission_mode="acceptEdits",
    )

    last_msg: ResultMessage | None = None
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, ResultMessage):
                last_msg = msg
    except Exception as exc:  # noqa: BLE001
        logger.exception("Validator session failed for step %s", step.id)
        return ValidationVerdict(
            passed=False,
            diagnosis=f"Validator session error: {exc}",
        )

    if last_msg is None:
        return ValidationVerdict(
            passed=False,
            diagnosis="No ResultMessage from validator SDK",
        )

    verdict = _parse_verdict(last_msg)
    if verdict is not None:
        return verdict

    # If no JSON found but the response mentions pass/success,
    # create a simple verdict from the text
    text = last_msg.result or ""
    text_lower = text.lower()
    if any(
        kw in text_lower
        for kw in ["passed", "passes", "success", "correct"]
    ):
        return ValidationVerdict(
            passed=True,
            diagnosis=text.strip()[:500],
        )

    return ValidationVerdict(
        passed=False,
        diagnosis=f"Validator produced no parseable verdict. "
        f"Raw: {text.strip()[:500]}",
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
        repo_path: Absolute path to the target repository.

    Returns:
        A ValidationVerdict indicating pass/fail and diagnosis.
    """
    level = step.validator.level
    logger.info("Validating step %s at level %d", step.id, level)

    if level == 1:
        return _validate_level_1(step, repo_path)

    return await _validate_level_2_to_5(
        step, result, cfg, repo_path, level,
    )
