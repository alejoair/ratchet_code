"""Hook builders for executor and validator sessions.

Implements procedural enforcement of project restrictions:
ruff_on_edit, commit_format_check, bash_whitelist, validator_no_write.
"""

import logging
from typing import Any

from ratchet.config import Config
from ratchet.plan.schema import Step

logger = logging.getLogger(__name__)


def _ruff_on_edit_hook() -> dict[str, Any]:
    """Build a PostToolUse hook that runs ruff on edited files.

    Returns:
        Hook dict for ClaudeAgentOptions.hooks.
    """
    return {
        "event": "PostToolUse",
        "matcher": ["Edit", "Write"],
        "action": "run",
        "command": "ruff check --fix {file_path}",
    }


def _commit_format_check_hook(
    pattern: str,
) -> dict[str, Any]:
    """Build a PreToolUse hook that validates commit message format.

    Args:
        pattern: Regex pattern for valid commit messages.

    Returns:
        Hook dict for ClaudeAgentOptions.hooks.
    """
    return {
        "event": "PreToolUse",
        "matcher": "Bash",
        "action": "check",
        "check": (
            "if the command matches 'git commit', "
            f"validate the message against /{pattern}/. "
            "Reject if it does not match."
        ),
    }


def _bash_whitelist_hook(
    allowlist: list[str],
) -> dict[str, Any]:
    """Build a PreToolUse hook that restricts bash commands.

    Args:
        allowlist: List of allowed command prefixes.

    Returns:
        Hook dict for ClaudeAgentOptions.hooks.
    """
    return {
        "event": "PreToolUse",
        "matcher": "Bash",
        "action": "check",
        "check": (
            "Only allow bash commands that start with "
            "one of: "
            f"{', '.join(allowlist)}. "
            "Reject anything else."
        ),
    }


def _validator_no_write_hook() -> dict[str, Any]:
    """Build a PreToolUse hook that rejects Edit and Write.

    Used in validator sessions to enforce read-only behavior.

    Returns:
        Hook dict for ClaudeAgentOptions.hooks.
    """
    return {
        "event": "PreToolUse",
        "matcher": ["Edit", "Write"],
        "action": "reject",
        "reason": (
            "Validators are read-only. "
            "Edit and Write tools are not allowed."
        ),
    }


def build_hooks(
    cfg: Config,
    step: Step,
    is_validator: bool = False,
) -> list[dict[str, Any]]:
    """Build the hook list for an executor or validator session.

    Args:
        cfg: Active configuration with hook settings.
        step: The step being executed or validated.
        is_validator: True if building hooks for a validator.

    Returns:
        List of hook dicts for ClaudeAgentOptions.hooks.
    """
    hooks: list[dict[str, Any]] = []

    if is_validator:
        hooks.append(_validator_no_write_hook())
        return hooks

    if cfg.hooks.ruff_on_edit:
        hooks.append(_ruff_on_edit_hook())

    if (
        cfg.hooks.commit_format_check
        and cfg.hooks.commit_message_pattern
    ):
        hooks.append(
            _commit_format_check_hook(
                cfg.hooks.commit_message_pattern,
            )
        )

    if cfg.hooks.bash_allowlist:
        hooks.append(
            _bash_whitelist_hook(cfg.hooks.bash_allowlist),
        )

    return hooks
