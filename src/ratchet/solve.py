"""Entry point for the ratchet solver.

Loads the planner session options, runs the Claude SDK query, and returns
the git diff produced by the session along with token/cost metrics.
"""

import logging
import subprocess
from dataclasses import dataclass

from claude_agent_sdk import ResultMessage, query

from ratchet.plan.planner import build_planner_options

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-6"


@dataclass
class SolveResult:
    """Result of a ratchet solve() call."""

    patch: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0


async def solve(
    repo_path: str,
    request: str,
    config_path: str | None,
    model: str = _DEFAULT_MODEL,
) -> SolveResult:
    """Run the ratchet planner on a repository and return the result.

    Args:
        repo_path: Absolute path to the target repository.
        request: Problem statement / task description.
        config_path: Path to ratchet.config.json
            (unused until config is wired).
        model: Claude model ID to use for the planner session.

    Returns:
        A SolveResult containing the patch and usage metrics.
    """
    options = build_planner_options(
        repo_path=repo_path, model=model,
    )

    last_result: ResultMessage | None = None
    async for message in query(prompt=request, options=options):
        if isinstance(message, ResultMessage):
            last_result = message

    diff = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )

    result = SolveResult(patch=diff.stdout)

    if last_result is not None:
        result.num_turns = getattr(
            last_result, "num_turns", 0,
        )
        result.cost_usd = (
            getattr(last_result, "total_cost_usd", 0.0) or 0.0
        )
        result.input_tokens = (
            getattr(last_result, "input_tokens", 0) or 0
        )
        result.output_tokens = (
            getattr(last_result, "output_tokens", 0) or 0
        )
        result.cache_read_tokens = (
            getattr(last_result, "cache_read_tokens", 0) or 0
        )
        result.cache_creation_tokens = (
            getattr(
                last_result, "cache_creation_tokens", 0,
            ) or 0
        )
        logger.info(
            "Planner session complete — "
            "turns: %d, cost: $%.4f",
            result.num_turns,
            result.cost_usd,
        )

    return result
