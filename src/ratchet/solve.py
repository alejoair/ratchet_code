"""Entry point for the ratchet solver.

async def solve(repo_path, request, config_path) -> str
    Loads config, instantiates stores, runs the orchestrator, returns git diff.
"""


async def solve(
    repo_path: str,
    request: str,
    config_path: str | None,
) -> str:
    """Run the ratchet solver on a repository.

    Args:
        repo_path: Absolute path to the target repository.
        request: Problem statement / task description.
        config_path: Optional path to ratchet.config.json.

    Returns:
        The git diff produced by the solver session.

    Raises:
        NotImplementedError: Until the orchestrator is wired up.
    """
    raise NotImplementedError("solve() not yet implemented")
