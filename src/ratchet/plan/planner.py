"""Planner session configuration for the ratchet orchestrator.

The planner is a Claude SDK client that receives the task description and
operates with the full Claude Code toolset and bypass permission mode.
The orchestrator owns the session lifecycle; this module only builds options.
"""

from claude_agent_sdk import ClaudeAgentOptions

# All standard Claude Code tools — no restrictions at the planner level.
# The planner needs full read/write/execute access to work on the repo.
PLANNER_TOOLS: list[str] = [
    "Bash",
    "Edit",
    "Glob",
    "Grep",
    "LS",
    "MultiEdit",
    "NotebookEdit",
    "NotebookRead",
    "Read",
    "TodoRead",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "Write",
]

_SYSTEM_PROMPT = """\
You are an autonomous software engineer working inside a Git repository.
Your goal is to fix the issue described in the task.

Guidelines:
- Read the relevant source files before making changes.
- Make the minimal set of changes required to fix the issue.
- Do NOT modify test files unless the issue explicitly requires it.
- Do NOT add unrelated refactors, comments, or formatting changes.
- After making changes, verify they are correct by re-reading the modified files.
"""


def build_planner_options(repo_path: str, model: str) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for a planner session.

    Args:
        repo_path: Absolute path to the repository being worked on.
        model: Claude model ID to use for the planner.

    Returns:
        ClaudeAgentOptions configured with all Claude Code tools,
        bypass permission mode, and project-level settings.
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
