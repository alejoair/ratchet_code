"""Orchestrator for the ratchet pipeline.

Starts the planner as a live SDK client, binds ContextVars, injects
the ratchet_catalog MCP server, and streams messages to/from the user.
"""

import json
import logging
import subprocess

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
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
from ratchet.solve import SolveResult
from ratchet.state import State

logger = logging.getLogger(__name__)


def _truncate(text: str, max_len: int) -> str:
    """Truncate text with ellipsis indicator."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _log_assistant_message(msg: AssistantMessage) -> None:
    """Log a structured summary of an AssistantMessage to INFO."""
    for block in msg.content:
        if isinstance(block, TextBlock):
            text = block.text.strip()
            if text:
                logger.info("[planner] %s", _truncate(text, 300))
        elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
            raw_input = block.input
            logger.info(
                "[planner] >> %s(%s)",
                block.name,
                _truncate(json.dumps(raw_input, default=str), 200),
            )
        elif isinstance(block, ToolResultBlock):
            content_str = _truncate(str(block.content), 200)
            tag = "ERR" if block.is_error else "OK"
            logger.info("[planner] << %s %s", tag, content_str)
        elif isinstance(block, ServerToolResultBlock):
            content_str = _truncate(json.dumps(block.content, default=str), 200)
            logger.info("[planner] << %s", content_str)


def _extract_metrics(
    result: SolveResult,
    last_msg: ResultMessage,
) -> None:
    """Populate metrics fields from a ResultMessage."""
    result.num_turns = last_msg.num_turns or 0
    result.cost_usd = last_msg.total_cost_usd or 0.0

    usage = last_msg.usage or {}
    result.input_tokens = usage.get("input_tokens", 0) or 0
    result.output_tokens = usage.get("output_tokens", 0) or 0
    result.cache_read_tokens = usage.get("cache_read_input_tokens", 0) or 0
    result.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0) or 0

_SYSTEM_PROMPT = """\
You are an autonomous software engineer working inside a Git repository.
Your goal is to fix the issue described in the task.

You have access to planning tools that let you define the task, create
a step-by-step plan, and execute steps one at a time. You CANNOT edit
files directly -- you must use the step() tool to execute each step,
which launches a separate executor agent with write access.

Workflow:
1. Use task_create to define the task.
2. Read the relevant source files to understand the codebase.
3. Use add_step to build a plan with clear goals and briefings.
4. Use submit_plan to lock the plan.
5. Use step() to execute each step sequentially.
6. Review the verdict after each step and adjust if needed.

Step error handling:
- When step() returns error_type="sdk_error", it means the
  SDK infrastructure failed (not a code problem).
  Do NOT create fix-up steps. Just retry the step once.
- When step() returns error_type=null and status="failed",
  the executor or validator found a real problem.
  Read the verdict diagnosis and adjust the plan.

Guidelines:
- Read the relevant source files BEFORE creating the plan.
- Make the minimal set of changes required to fix the issue.
- Do NOT modify test files unless the issue explicitly requires it.
- Do NOT add unrelated refactors, comments, or formatting changes.
- Do NOT create test files, documentation files, or README files.
- Do NOT delete any existing files.
- Only modify the minimum necessary source files.
- Prefer minimal, surgical changes over refactors.
- Each step briefing must be self-contained: include file paths,
  line numbers, and exact instructions for the executor.
- The executor has NO memory between steps -- include all context
  the executor needs in the step briefing.
- For implement_step, optionally specify function_signatures, imports, classes, or code_snippets to make the implementation scope more explicit.
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

    # The CLI prefixes MCP tool names as mcp__<server>__<tool>.
    # Both 'tools' and 'allowed_tools' must use the prefixed names
    # for the CLI to recognize and auto-approve them.
    mcp_prefix = "mcp__ratchet_catalog__"
    prefixed_mcp_tools = [
        mcp_prefix + name for name in ALL_TOOL_NAMES
    ]
    all_tools = PLANNER_TOOLS + prefixed_mcp_tools

    options = ClaudeAgentOptions(
        model=model,
        cwd=repo_path,
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
        elif isinstance(message, AssistantMessage):
            _log_assistant_message(message)

    diff = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )

    result = SolveResult(
        patch=diff.stdout,
        plan_trace=await plan_store.view(),
    )
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
