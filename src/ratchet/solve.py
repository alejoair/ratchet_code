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

    # Resolve config: explicit path > <repo_path>/ratchet.config.json > defaults
    cfg: Config | None = None
    resolved_path = config_path
    if resolved_path is None:
        from pathlib import Path  # noqa: PLC0415

        candidate = Path(repo_path) / "ratchet.config.json"
        if candidate.is_file():
            resolved_path = str(candidate)
            logger.info("Using config from %s", resolved_path)

    if resolved_path is not None:
        try:
            cfg = Config.load(resolved_path)
        except (FileNotFoundError, ValueError):
            logger.warning(
                "Could not load config from %s, "
                "using defaults",
                resolved_path,
            )

    if cfg is None:
        cfg = Config(
            models={"planner": model},
        )

    # Parse CLAUDE.md restrictions and inject into config
    try:
        from ratchet.claude_md import (  # noqa: PLC0415
            parse_claude_md,
        )
        claude_md = parse_claude_md(repo_path)
        cfg.restrictions = claude_md.restrictions
        logger.info("Loaded CLAUDE.md restrictions (%d chars)", len(cfg.restrictions))
    except (FileNotFoundError, ValueError) as exc:
        logger.info("No CLAUDE.md restrictions loaded: %s", exc)

    return await run_orchestrated(
        repo_path=repo_path,
        request=request,
        cfg=cfg,
        model=model,
    )
