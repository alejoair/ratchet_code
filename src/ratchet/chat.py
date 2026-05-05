"""Interactive chat REPL for the ratchet planner."""

import json
import logging
import re

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from ratchet.config import Config
from ratchet.plan.catalog import (
    ALL_TOOL_NAMES,
    bind_catalog_context,
    build_catalog_server,
)
from ratchet.plan.planner import PLANNER_TOOLS
from ratchet.plan.store import PlanStore, TaskStore
from ratchet.state import State

logger = logging.getLogger(__name__)

_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"

_ = re.sub  # used below
_ = json.dumps  # used below
_ = AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient
_ = RateLimitEvent, ResultMessage, StreamEvent, SystemMessage
_ = ServerToolResultBlock, ServerToolUseBlock
_ = TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock
_ = Config, ALL_TOOL_NAMES, bind_catalog_context
_ = build_catalog_server, PLANNER_TOOLS, PlanStore, TaskStore, State


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return re.sub(r"\033\[[0-9;]*m", "", text)


def _truncate(text: str, max_len: int = 300) -> str:
    """Truncate text with ellipsis."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _render_text_block(block: TextBlock) -> None:
    """Render a TextBlock to stdout."""
    text = block.text.strip()
    if text:
        print(f"{_BOLD}{text}{_RESET}")


def _render_thinking_block(block: ThinkingBlock) -> None:
    """Render a ThinkingBlock to stdout."""
    text = block.thinking.strip()
    if not text:
        return
    lines = text.split("\n")
    preview = "\n".join(lines[:5])
    if len(lines) > 5:
        extra = len(lines) - 5
        preview += f"\n{_DIM}... ({extra} more lines){_RESET}"
    header = f"{_DIM}{_YELLOW}[thinking]{_RESET}"
    print(f"\n{header}\n{_DIM}{preview}{_RESET}")


def _render_tool_use(
    block: ToolUseBlock | ServerToolUseBlock,
) -> None:
    """Render a tool use block to stdout."""
    short_name = block.name.replace(
        "mcp__ratchet_catalog__", "",
    )
    raw = json.dumps(block.input, default=str)
    display = _truncate(_truncate(raw, 200), 120)
    print(
        f"\n{_CYAN}>> {short_name}{_RESET} "
        f"{_DIM}({display}){_RESET}"
    )


def _render_tool_result(block: ToolResultBlock) -> None:
    """Render a tool result block to stdout."""
    tag = f"{_RED}ERR" if block.is_error else f"{_GREEN}OK"
    content = block.content
    if isinstance(content, str):
        text = _truncate(content, 200)
    elif isinstance(content, list):
        text = _truncate(json.dumps(content, default=str), 200)
    else:
        text = (
            "(empty)" if content is None
            else _truncate(str(content), 200)
        )
    print(f"{_CYAN}<< {tag}{_RESET} {text}")


def _render_server_result(
    block: ServerToolResultBlock,
) -> None:
    """Render a server tool result block to stdout."""
    content_str = _truncate(
        json.dumps(block.content, default=str), 200,
    )
    print(f"{_CYAN}<< {_GREEN}OK{_RESET} {content_str}")


def _render_assistant_message(
    msg: AssistantMessage,
) -> None:
    """Render all blocks in an AssistantMessage."""
    for block in msg.content:
        if isinstance(block, ThinkingBlock):
            _render_thinking_block(block)
        elif isinstance(block, TextBlock):
            _render_text_block(block)
        elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
            _render_tool_use(block)
        elif isinstance(block, ToolResultBlock):
            _render_tool_result(block)
        elif isinstance(block, ServerToolResultBlock):
            _render_server_result(block)


def _render_result_message(msg: ResultMessage) -> None:
    """Render the final ResultMessage summary."""
    cost = msg.total_cost_usd or 0.0
    turns = msg.num_turns or 0
    duration = msg.duration_ms or 0
    usage = msg.usage or {}
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    tag = (
        f"{_RED}error{_RESET}" if msg.is_error
        else f"{_GREEN}done{_RESET}"
    )
    seconds = duration / 1000
    print(
        f"\n{_BOLD}[{tag}]{_RESET} "
        f"turns={turns} tokens={inp}+{out} "
        f"cost=${cost:.4f} duration={seconds:.1f}s"
    )


def _render_system_message(msg: SystemMessage) -> None:
    """Render a system notification."""
    text = (
        str(msg) if not isinstance(msg, dict)
        else json.dumps(msg)
    )
    print(
        f"{_DIM}{_YELLOW}[system] "
        f"{_truncate(text, 200)}{_RESET}"
    )


def _render_rate_limit(event: RateLimitEvent) -> None:
    """Render a rate limit event."""
    info = event.rate_limit_info
    ms = getattr(info, "resets_in_ms", "?")
    print(
        f"{_YELLOW}[rate-limit] "
        f"resets in {ms}ms{_RESET}"
    )


def _render_stream_event(event: StreamEvent) -> None:
    """Render a streaming event (suppressed for readability)."""
    _ = event  # stream events are too noisy to display


_PLANNER_SYSTEM_PROMPT = (
    "You are an autonomous software engineer working "
    "inside a Git repository. Your goal is to help the "
    "user understand and modify the codebase.\n\n"
    "You have access to planning tools that let you "
    "define tasks, create step-by-step plans, and "
    "execute steps one at a time. You CANNOT edit "
    "files directly -- you must use the step() tool "
    "to execute each step, which launches a separate "
    "executor agent with write access.\n\n"
    "Workflow:\n"
    "1. Use task_create to define the task.\n"
    "2. Read the relevant source files.\n"
    "3. Use add_step to build a plan.\n"
    "4. Use submit_plan to lock the plan.\n"
    "5. Use step() to execute each step.\n"
    "6. Review the verdict and adjust.\n\n"
    "Guidelines:\n"
    "- Read files BEFORE creating the plan.\n"
    "- Make minimal changes to fix the issue.\n"
    "- Each step briefing must be self-contained.\n"
    "- The executor has NO memory between steps.\n"
)


def _read_input() -> str | None:
    """Read a line of input, returning None on EOF."""
    try:
        return input("> ")
    except (EOFError, KeyboardInterrupt):
        return None


async def run_chat(
    repo_path: str,
    cfg: Config,
    model: str,
) -> None:
    """Run an interactive chat session with the ratchet planner.

    Args:
        repo_path: Absolute path to the target repository.
        cfg: Active configuration.
        model: Claude model ID for the planner.
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

    mcp_prefix = "mcp__ratchet_catalog__"
    prefixed = [
        mcp_prefix + n for n in ALL_TOOL_NAMES
    ]
    all_tools = PLANNER_TOOLS + prefixed

    options = ClaudeAgentOptions(
        model=model,
        cwd=repo_path,
        system_prompt=_PLANNER_SYSTEM_PROMPT,
        tools=all_tools,
        allowed_tools=all_tools,
        mcp_servers={"ratchet_catalog": catalog},
        permission_mode="acceptEdits",
        max_turns=cfg.budgets.max_planner_turns,
    )

    print(
        f"\n{_BOLD}{_CYAN}ratchet chat{_RESET} "
        f"model={model} repo={repo_path}"
    )
    print(
        f"{_DIM}Type your message and press Enter. "
        f"Ctrl+C or Ctrl+D to exit.{_RESET}\n"
    )

    client = ClaudeSDKClient(options=options)
    await client.connect()

    try:
        while True:
            user_input = _read_input()
            if user_input is None:
                print(f"\n{_DIM}Goodbye.{_RESET}")
                break
            user_input = user_input.strip()
            if not user_input:
                continue

            print()
            await client.query(prompt=user_input)

            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    _render_assistant_message(message)
                elif isinstance(message, ResultMessage):
                    _render_result_message(message)
                elif isinstance(message, SystemMessage):
                    _render_system_message(message)
                elif isinstance(message, StreamEvent):
                    _render_stream_event(message)
                elif isinstance(message, RateLimitEvent):
                    _render_rate_limit(message)

            print()
    except KeyboardInterrupt:
        print(f"\n{_DIM}Interrupted.{_RESET}")
    finally:
        await client.disconnect()
