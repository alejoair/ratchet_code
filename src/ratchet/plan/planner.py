"""Planner session configuration for the ratchet orchestrator.

The planner is a Claude SDK client that receives the task description,
creates a plan using catalog tools, and delegates execution to the
step() tool. The planner only has read-only access to the repo --
all writes happen through the executor launched by step().
"""

from claude_agent_sdk import ClaudeAgentOptions

# Read-only Claude Code tools. The planner can explore the repo
# but cannot modify it directly. All modifications must go through
# step() which launches a separate executor with write access.
PLANNER_TOOLS: list[str] = [
    "Glob",
    "Grep",
    "LS",
    "Read",
]

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

Guidelines:
- Read the relevant source files BEFORE creating the plan.
- Make the minimal set of changes required to fix the issue.
- Do NOT modify test files unless the issue requires it.
- Do NOT add unrelated refactors, comments, or formatting changes.
- Each step briefing must be self-contained: include file paths,
  line numbers, and exact instructions for the executor.
- The executor has NO memory between steps -- include all context
  the executor needs in the step briefing.
- For implement_step, optionally specify function_signatures, imports, classes, or code_snippets to make the implementation scope more explicit.
"""


def build_planner_options(repo_path: str, model: str) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for a planner session.

    Args:
        repo_path: Absolute path to the repository being worked on.
        model: Claude model ID to use for the planner.

    Returns:
        ClaudeAgentOptions configured with read-only Claude Code
        tools, catalog MCP tools, and project-level settings.
    """
    return ClaudeAgentOptions(
        model=model,
        cwd=repo_path,
        system_prompt=_SYSTEM_PROMPT,
        tools=PLANNER_TOOLS,
        allowed_tools=PLANNER_TOOLS,
        permission_mode="acceptEdits",
        setting_sources=["user", "project", "local"],
    )
