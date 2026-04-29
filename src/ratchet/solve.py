"""Entry point for the ratchet solver.

Loads the planner session options, runs the Claude SDK query, and returns
the git diff produced by the session.
"""

import asyncio
import logging
import subprocess

from claude_agent_sdk import ResultMessage, query

from ratchet.plan.planner import build_planner_options

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-6"


async def solve(
    repo_path: str,
    request: str,
    config_path: str | None,
    model: str = _DEFAULT_MODEL,
) -> str:
    """Run the ratchet planner on a repository and return the resulting git diff.

    Args:
        repo_path: Absolute path to the target repository.
        request: Problem statement / task description.
        config_path: Path to ratchet.config.json (unused until config is wired).
        model: Claude model ID to use for the planner session.

    Returns:
        The output of ``git diff HEAD`` after the session completes.
    """
    options = build_planner_options(repo_path=repo_path, model=model)

    async for message in query(prompt=request, options=options):
        if isinstance(message, ResultMessage):
            logger.info(
                "Planner session complete — turns: %d, cost: $%.4f",
                message.num_turns,
                message.total_cost_usd or 0.0,
            )

    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout
