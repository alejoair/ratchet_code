"""Entry point for the ratchet solver.

Runs the full orchestrated pipeline with catalog MCP,
executor, validator, and plan/step management.
"""

import logging
from dataclasses import dataclass

from ratchet.config import Config

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
    plan_trace: dict | None = None


async def solve(
    repo_path: str,
    request: str,
    config_path: str | None,
    model: str = _DEFAULT_MODEL,
) -> SolveResult:
    """Run the ratchet solver on a repository.

    Args:
        repo_path: Absolute path to the target repository.
        request: Problem statement / task description.
        config_path: Path to ratchet.config.json.
        model: Claude model ID for the planner session.

    Returns:
        A SolveResult containing the patch and usage
        metrics.
    """
    from ratchet.orchestrator import (  # noqa: PLC0415
        run_orchestrated,
    )

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

    if cfg is None:
        cfg = Config(
            models={"planner": model},
        )

    return await run_orchestrated(
        repo_path=repo_path,
        request=request,
        cfg=cfg,
        model=model,
    )
