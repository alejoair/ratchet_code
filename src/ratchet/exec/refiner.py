"""Plan refinement agent for the ratchet pipeline.

Runs on submit_plan to evaluate plan quality against a rubric.
Returns a RefinementVerdict with approval, score, and
actionable recommendations via the SDK's output_format mechanism.
"""

import logging
import os
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    query,
)

from ratchet.config import Config
from ratchet.plan.schema import (
    RefinementVerdict,
    Step,
    sdk_output_schema,
)

logger = logging.getLogger(__name__)

_CLEAN_ENV = {
    k: v for k, v in os.environ.items()
    if k != "CLAUDECODE"
}

_REFINER_SYSTEM_PROMPT = (
    "You are a plan quality reviewer. "
    "You receive a plan (list of steps) and a rubric. "
    "You evaluate the plan against the rubric and "
    "return a structured verdict.\n\n"
    "You do NOT have access to any tools. "
    "You evaluate purely based on the plan data "
    "provided."
)


def _render_plan_text(
    steps: list[Step],
    rationale: str,
) -> str:
    """Render plan steps as readable text for the refiner.

    Args:
        steps: All steps in the plan.
        rationale: The planner's submit rationale.

    Returns:
        Formatted text describing the plan.
    """
    lines: list[str] = [
        f"Planner rationale: {rationale}",
        "",
        f"Plan ({len(steps)} steps):",
        "",
    ]
    for i, s in enumerate(steps, 1):
        lines.append(f"--- Step {i}: {s.id} ---")
        lines.append(f"Type: {s.type.value}")
        lines.append(f"Goal: {s.goal}")
        if s.intent:
            lines.append(
                f"Intent: {s.intent.value}"
            )
        if s.depends_on:
            lines.append(
                f"Depends on: "
                f"{', '.join(s.depends_on)}"
            )
        if s.target_files:
            lines.append(
                f"Target files: "
                f"{', '.join(s.target_files)}"
            )
        if s.creates_files:
            lines.append(
                f"Creates files: "
                f"{', '.join(s.creates_files)}"
            )
        if s.deletes_files:
            lines.append(
                f"Deletes files: "
                f"{', '.join(s.deletes_files)}"
            )
        lines.append(
            f"Validator level: "
            f"{s.validator.level}"
        )
        lines.append(
            f"Success criterion: "
            f"{s.validator.success_criterion}"
        )
        lines.append(f"Briefing: {s.briefing}")
        lines.append("")
    return "\n".join(lines)


async def refine(
    steps: list[Step],
    rationale: str,
    cfg: Config,
    repo_path: str,
) -> RefinementVerdict:
    """Run the plan refinement agent.

    Evaluates the plan against the configured rubric and
    returns a RefinementVerdict with score and
    recommendations.

    Args:
        steps: All steps in the submitted plan.
        rationale: The planner's submit rationale.
        cfg: Active configuration.
        repo_path: Absolute path to the repository.

    Returns:
        A RefinementVerdict with approval decision.
    """
    refiner_cfg = cfg.refiner
    model = refiner_cfg.model
    if not model:
        logger.warning(
            "Refiner enabled but no model; "
            "auto-approving plan"
        )
        return RefinementVerdict(
            approved=True,
            score=100,
            diagnosis=(
                "Refiner has no model, "
                "auto-approving"
            ),
        )

    # Load rubric
    rubric_path = refiner_cfg.rubric_path
    if os.path.isabs(rubric_path):
        rubric_file = rubric_path
    else:
        rubric_file = os.path.join(
            repo_path, rubric_path,
        )
    try:
        rubric_text = Path(
            rubric_file,
        ).read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning(
            "Rubric file not found at %s",
            rubric_file,
        )
        rubric_text = ""

    # Build prompt
    plan_text = _render_plan_text(steps, rationale)
    prompt = (
        f"## Rubric\n\n{rubric_text}\n\n"
        f"## Plan to evaluate\n\n"
        f"{plan_text}"
    )

    output_schema = sdk_output_schema(
        RefinementVerdict,
    )

    # Build options (no tools)
    options = ClaudeAgentOptions(
        model=model,
        cwd=repo_path,
        system_prompt=_REFINER_SYSTEM_PROMPT,
        tools=[],
        allowed_tools=[],
        max_turns=refiner_cfg.max_turns,
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
                    "Refiner msg type=%s",
                    type(msg).__name__,
                )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Refiner session failed: %s", exc,
        )
        return RefinementVerdict(
            approved=True,
            score=100,
            diagnosis=(
                "Refiner session failed, "
                f"auto-approving: {exc}"
            ),
        )

    if last_msg is None:
        return RefinementVerdict(
            approved=False,
            score=0,
            diagnosis=(
                "No ResultMessage from refiner"
            ),
        )

    # Read structured output from SDK
    raw = last_msg.structured_output
    if raw is not None:
        try:
            verdict = (
                RefinementVerdict.model_validate(
                    raw,
                )
            )
            if verdict.score < refiner_cfg.min_score:
                verdict.approved = False
            logger.info(
                "Refiner verdict: approved=%s "
                "score=%d for %d steps",
                verdict.approved,
                verdict.score,
                len(steps),
            )
            return verdict
        except ValueError:
            logger.warning(
                "Refiner structured_output "
                "failed validation"
            )

    return RefinementVerdict(
        approved=False,
        score=0,
        diagnosis=(
            "Refiner produced no structured "
            f"output. Raw: "
            f"{(last_msg.result or '')[:500]}"
        ),
    )
