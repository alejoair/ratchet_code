"""Entry point for the ratchet solver.

Supports two modes:
  - planner-only: direct SDK query with full toolset (default).
  - orchestrated: full pipeline with catalog MCP, executor,
    validator, and plan/step management.
"""

import logging
import subprocess
from dataclasses import dataclass

from claude_agent_sdk import ResultMessage, query

from ratchet.config import Config
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


def _extract_metrics(
    result: SolveResult,
    last_msg: ResultMessage,
) -> None:
    """Populate metrics fields from a ResultMessage.

    The SDK exposes token counts inside ``last_msg.usage``
    (a dict), not as direct attributes.

    Args:
        result: The SolveResult to fill.
        last_msg: The final ResultMessage from the SDK.
    """
    result.num_turns = last_msg.num_turns or 0
    result.cost_usd = last_msg.total_cost_usd or 0.0

    usage = last_msg.usage or {}
    result.input_tokens = usage.get(
        "input_tokens", 0,
    ) or 0
    result.output_tokens = usage.get(
        "output_tokens", 0,
    ) or 0
    result.cache_read_tokens = usage.get(
        "cache_read_input_tokens", 0,
    ) or 0
    result.cache_creation_tokens = usage.get(
        "cache_creation_input_tokens", 0,
    ) or 0


async def solve(
    repo_path: str,
    request: str,
    config_path: str | None,
    model: str = _DEFAULT_MODEL,
    *,
    orchestrated: bool = False,
) -> SolveResult:
    """Run the ratchet solver on a repository.

    Args:
        repo_path: Absolute path to the target repository.
        request: Problem statement / task description.
        config_path: Path to ratchet.config.json.
        model: Claude model ID for the planner session.
        orchestrated: If True, use the full pipeline
            with catalog MCP, executor, and validator.

    Returns:
        A SolveResult containing the patch and usage
        metrics.
    """
    # Load config if available
    cfg: Config | None = None
    if config_path is not None:
        try:
            cfg = Config.load(config_path)
        except (FileNotFoundError, ValueError):
            logger.warning(
                "Could not load config from %s, "
                "using defaults",
                config_path,
            )

    if orchestrated:
        return await _solve_orchestrated(
            repo_path, request, cfg, model,
        )
    return await _solve_planner_only(
        repo_path, request, model,
    )


async def _solve_planner_only(
    repo_path: str,
    request: str,
    model: str,
) -> SolveResult:
    """Run planner-only mode (no catalog/executor).

    Args:
        repo_path: Absolute path to the target repo.
        request: Problem statement.
        model: Claude model ID.

    Returns:
        SolveResult with patch and metrics.
    """
    options = build_planner_options(
        repo_path=repo_path, model=model,
    )

    last_result: ResultMessage | None = None
    async for message in query(
        prompt=request, options=options,
    ):
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
        _extract_metrics(result, last_result)
        logger.info(
            "Planner session complete — "
            "turns: %d, cost: $%.4f",
            result.num_turns,
            result.cost_usd,
        )

    return result


async def _solve_orchestrated(
    repo_path: str,
    request: str,
    cfg: Config | None,
    model: str,
) -> SolveResult:
    """Run orchestrated mode with full pipeline.

    Args:
        repo_path: Absolute path to the target repo.
        request: Problem statement.
        cfg: Loaded Config or None for defaults.
        model: Claude model ID.

    Returns:
        SolveResult with patch (no per-step metrics
        available in orchestrated mode yet).
    """
    from ratchet.orchestrator import (  # noqa: PLC0415
        run_orchestrated,
    )

    if cfg is None:
        cfg = Config(
            models={"planner": model},
        )

    patch = await run_orchestrated(
        repo_path=repo_path,
        request=request,
        cfg=cfg,
        model=model,
    )

    return SolveResult(patch=patch)
