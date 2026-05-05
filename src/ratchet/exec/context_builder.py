"""Context builder agent for the ratchet pipeline.

Runs before each step execution to enrich the step briefing
with precise, repo-grounded information: function signatures,
import dependencies, file relationships, and other relevant
context that the executor agent needs.
"""

import logging
import os

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    query,
)

from ratchet.config import Config
from ratchet.plan.schema import (
    ContextResult,
    Step,
    StepOutput,
    sdk_output_schema,
)

logger = logging.getLogger(__name__)

_CLEAN_ENV = {
    k: v for k, v in os.environ.items()
    if k != "CLAUDECODE"
}

_CONTEXT_TOOLS = ["Read", "Grep", "Glob"]

_CONTEXT_BUILDER_PROMPT = (
    "You are a code context researcher. "
    "Your job is to gather precise information "
    "about the code that will be modified in a "
    "planned step.\n\n"
    "You have read-only tools (Read, Grep, Glob) "
    "to inspect the repository.\n\n"
    "Given a step description with target files, "
    "you must find and report:\n"
    "1. Function and class signatures in target "
    "files (exact signatures, not summaries).\n"
    "2. Imports and dependencies between the "
    "target files and other project files.\n"
    "3. Any tests related to the target files.\n"
    "4. Whether documentation updates are needed.\n"
    "5. Any other context relevant to the step."
)


def _render_builder_prompt(
    step: Step,
    deps: dict[str, StepOutput],
) -> str:
    """Render the prompt for the context builder.

    Args:
        step: The step to build context for.
        deps: Outputs from prerequisite steps.

    Returns:
        The rendered prompt string.
    """
    parts: list[str] = []

    parts.append(
        f"## Step to prepare context for\n"
        f"ID: {step.id}\n"
        f"Type: {step.type.value}\n"
        f"Goal: {step.goal}\n"
    )

    if step.briefing:
        parts.append(
            f"## Planner briefing\n"
            f"{step.briefing}\n"
        )

    if step.target_files:
        parts.append(
            "## Target files (to modify)\n"
            + "\n".join(
                f"- {f}" for f in step.target_files
            )
        )

    if step.creates_files:
        parts.append(
            "## Files to create\n"
            + "\n".join(
                f"- {f}" for f in step.creates_files
            )
        )

    if step.deletes_files:
        parts.append(
            "## Files to delete\n"
            + "\n".join(
                f"- {f}" for f in step.deletes_files
            )
        )

    if step.function_signatures:
        parts.append(
            "## Declared function signatures\n"
            + "\n".join(
                f"- {s}"
                for s in step.function_signatures
            )
        )

    if deps:
        dep_lines: list[str] = [
            "## Context from previous steps",
        ]
        for sid, out in deps.items():
            dep_lines.append(
                f"### [{sid}] {out.summary}"
            )
            if out.notes:
                dep_lines.append(
                    f"Notes: {out.notes}"
                )
            if out.artifacts:
                for k, v in out.artifacts.items():
                    dep_lines.append(f"{k}: {v}")
        parts.append("\n".join(dep_lines))

    return "\n\n".join(parts)


async def build_context(
    step: Step,
    cfg: Config,
    repo_path: str,
    deps: dict[str, StepOutput],
) -> ContextResult:
    """Run the context builder agent.

    Inspects the repository to gather function
    signatures, import dependencies, test files,
    and other context relevant to the step.

    Args:
        step: The step to build context for.
        cfg: Active configuration.
        repo_path: Absolute path to the repo.
        deps: Outputs from prerequisite steps.

    Returns:
        A ContextResult with enriched context.
    """
    builder_cfg = cfg.context_builder
    model = builder_cfg.model
    if not model:
        logger.warning(
            "Context builder enabled but no model; "
            "returning empty context"
        )
        return ContextResult(enriched_context="")

    prompt = _render_builder_prompt(step, deps)
    output_schema = sdk_output_schema(ContextResult)

    options = ClaudeAgentOptions(
        model=model,
        cwd=repo_path,
        system_prompt=_CONTEXT_BUILDER_PROMPT,
        tools=_CONTEXT_TOOLS,
        allowed_tools=_CONTEXT_TOOLS,
        max_turns=builder_cfg.max_turns,
        permission_mode="acceptEdits",
        env=_CLEAN_ENV,
        output_format={
            "type": "json_schema",
            "schema": output_schema,
        },
    )

    last_msg: ResultMessage | None = None
    try:
        async for msg in query(
            prompt=prompt, options=options,
        ):
            if isinstance(msg, ResultMessage):
                last_msg = msg
            else:
                logger.debug(
                    "Context builder msg type=%s",
                    type(msg).__name__,
                )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Context builder failed: %s", exc,
        )
        return ContextResult(
            enriched_context="",
            notes=(
                f"Context builder failed: {exc}"
            ),
        )

    if last_msg is None:
        return ContextResult(
            enriched_context="",
            notes=(
                "No result from context builder"
            ),
        )

    # Read structured output from SDK
    raw = last_msg.structured_output
    if raw is not None:
        try:
            result = ContextResult.model_validate(
                raw,
            )
            logger.info(
                "Context builder for step %s: "
                "%d files, %d functions",
                step.id,
                len(result.files_read),
                len(result.functions_found),
            )
            return result
        except ValueError:
            logger.warning(
                "Context builder structured_output "
                "failed validation for step %s",
                step.id,
            )

    return ContextResult(
        enriched_context="",
        notes=(
            "Context builder produced no "
            "structured output"
        ),
    )
