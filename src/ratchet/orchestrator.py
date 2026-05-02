"""Orchestrator for the ratchet pipeline.

Starts the planner as a live SDK client, binds ContextVars, injects
the ratchet_catalog MCP server, and streams messages to/from the user.
"""

import logging
import subprocess

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    query,
)

from ratchet.config import Config
from ratchet.plan.catalog import (
    ALL_TOOL_NAMES,
    bind_catalog_context,
    build_catalog_server,
)
from ratchet.plan.planner import PLANNER_TOOLS
from ratchet.plan.store import PlanStore, TaskStore
from ratchet.solve import SolveResult, _extract_metrics
from ratchet.state import State

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are an autonomous software engineer working inside a Git repository.
Your goal is to fix the issue described in the task.

You have access to planning tools that let you define the task, create
a step-by-step plan, and execute steps one at a time.

Workflow:
1. Use task_create to define the task.
2. Use add_*_step tools to build a plan with clear goals.
3. Use submit_plan to lock the plan.
4. Use step() to execute each step sequentially.
5. Review the verdict after each step and adjust if needed.

Guidelines:
- Read the relevant source files before making changes.
- Make the minimal set of changes required to fix the issue.
- Do NOT modify test files unless the issue requires it.
- Do NOT add unrelated refactors, comments, or formatting changes.
"""


async def run_orchestrated(
    repo_path: str,
    request: str,
    cfg: Config,
    model: str,
) -> SolveResult:
    """Run the full ratchet pipeline with orchestrator.

    Creates the stores, binds ContextVars, builds the planner
    options with the ratchet_catalog MCP server injected, and
    runs the planner session. Returns the git diff after session
    completion.

    Args:
        repo_path: Absolute path to the target repository.
        request: Problem statement / task description.
        cfg: Active configuration.
        model: Claude model ID for the planner.

    Returns:
        A SolveResult with patch and usage metrics.
    """
    task_store = TaskStore()
    plan_store = PlanStore()
    state = State()

    bind_catalog_context(
        task_store=task_store,
        plan_store=plan_store,
        state=state,
        repo_path=repo_path,
        cfg=cfg,
    )

    catalog = build_catalog_server()

    all_tools = PLANNER_TOOLS + ALL_TOOL_NAMES

    options = ClaudeAgentOptions(
        model=model,
        cwd=repo_path,
        setting_sources=["user", "project", "local"],
        system_prompt=_SYSTEM_PROMPT,
        tools=all_tools,
        allowed_tools=all_tools,
        mcp_servers={"ratchet_catalog": catalog},
        permission_mode="acceptEdits",
        max_turns=cfg.budgets.max_planner_turns,
    )

    last_result: ResultMessage | None = None
    async for message in query(
        prompt=request, options=options,
    ):
        if isinstance(message, ResultMessage):
            last_result = message
        logger.debug(
            "Orchestrator message: %s",
            type(message).__name__,
        )

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
            "Orchestrator session complete — "
            "turns: %d, cost: $%.4f",
            result.num_turns,
            result.cost_usd,
        )
    else:
        logger.info("Orchestrator session complete (no metrics)")

    return result
