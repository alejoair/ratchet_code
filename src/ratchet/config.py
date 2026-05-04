"""Configuration models for ratchet.

Loads from ``ratchet.config.json`` and provides model lookups
for executor and validator sessions by step type and validation level.
"""

import json
import logging
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, Field

from ratchet.plan.schema import StepType

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_NAME = "ratchet.config.json"
_FALLBACK_MODEL = "claude-sonnet-4-6"


class ExecutorModels(BaseModel):
    """Model selection for executor sessions.

    Attributes:
        default: Fallback model for any step type without an override.
        implement_step: Model override for implement steps.
        update_docs_step: Model override for update_docs steps.
    """

    default: str
    implement_step: str | None = None
    update_docs_step: str | None = None

    def for_step_type(self, step_type: StepType) -> str:
        """Resolve the model for a given step type.

        Args:
            step_type: The step type to look up.

        Returns:
            The override model if set, otherwise the default.
        """
        override: str | None = getattr(
            self, step_type.value, None,
        )
        return override or self.default


class ValidationConfig(BaseModel):
    """Validation settings shared across all steps.

    Attributes:
        default_level: Default validation level when not specified.
        max_turns_by_level: Maximum LLM turns per validation level.
        level_by_step_type: Default validation level per step type.
            Overrides default_level when the step type has an entry.
    """

    default_level: Annotated[int, Field(ge=1, le=5)] = 3
    max_turns_by_level: dict[str, int] = {
        "1": 0,
        "2": 1,
        "3": 3,
        "4": 5,
        "5": 10,
    }
    level_by_step_type: dict[str, int] = {
        "discovery_step": 1,
        "implement_step": 3,
        "simple_task_step": 2,
        "verify_step": 1,
        "update_docs_step": 2,
    }


class BudgetConfig(BaseModel):
    """Global budget limits for a single solve session.

    Attributes:
        max_executor_turns: Max turns per executor step.
        max_planner_turns: Max turns for the planner session.
        max_total_steps: Max number of steps in a plan.
    """

    max_executor_turns: int = 20
    max_planner_turns: int = 200
    max_total_steps: int = 50


class HooksConfig(BaseModel):
    """Hook configuration for executor sessions.

    Attributes:
        ruff_on_edit: Run ruff format/check after edits.
        commit_format_check: Validate commit message format.
        bash_allowlist: Shell commands allowed without prompting.
        commit_message_pattern: Regex for commit message validation.
    """

    ruff_on_edit: bool = True
    commit_format_check: bool = True
    bash_allowlist: list[str] = []
    commit_message_pattern: str = ""


class Config(BaseModel):
    """Top-level ratchet configuration.

    Loaded from ``ratchet.config.json`` and used throughout the
    executor, validator, hooks, and orchestrator modules.

    Attributes:
        models: Model selection for planner, executor, and validator.
        validation: Default validation level and turn budgets.
        budgets: Global limits for executor and planner sessions.
        hooks: Hook configuration for code quality enforcement.
        restrictions: Parsed restrictions from CLAUDE.md, injected
            into executor system prompts.
    """

    models: dict[str, Any]
    validation: ValidationConfig = ValidationConfig()
    budgets: BudgetConfig = BudgetConfig()
    hooks: HooksConfig = HooksConfig()
    restrictions: str = ""

    @classmethod
    def load(cls, path: str = _DEFAULT_CONFIG_NAME) -> "Config":
        """Load configuration from a JSON file.

        Args:
            path: Path to the config JSON file.

        Returns:
            A validated Config instance.

        Raises:
            FileNotFoundError: If the config file does not exist.
            json.JSONDecodeError: If the file is not valid JSON.
        """
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
        logger.debug("Config loaded from %s", path)
        return cls.model_validate(data)

    def executor_model_for(self, step_type: StepType) -> str:
        """Resolve the executor model for a step type.

        Args:
            step_type: The step type to look up.

        Returns:
            The model ID to use for the executor session.
        """
        executor_cfg = self.models.get("executor")
        if isinstance(executor_cfg, dict):
            executor = ExecutorModels.model_validate(
                executor_cfg,
            )
            return executor.for_step_type(step_type)
        planner = self.models.get("planner")
        if isinstance(planner, str):
            return planner
        return _FALLBACK_MODEL

    def validator_model_for(self, level: int) -> str | None:
        """Resolve the validator model for a validation level.

        Args:
            level: The validation level (1-5).

        Returns:
            The model ID for the given level, or None if level 1
            (subprocess-based validation needs no model).
        """
        validator_cfg = self.models.get(
            "validator_by_level",
        )
        if isinstance(validator_cfg, dict):
            value = validator_cfg.get(str(level))
            return value if isinstance(value, str) else None
        return None

    def max_validator_turns(self, level: int) -> int:
        """Return the max turns for a validation level.

        Args:
            level: The validation level (1-5).

        Returns:
            Max turns for that level, or 0 if not configured.
        """
        return self.validation.max_turns_by_level.get(
            str(level), 0,
        )

    def default_validation_level(
        self, step_type: StepType,
    ) -> int:
        """Return the default validation level for a step type.

        Args:
            step_type: The step type to look up.

        Returns:
            The configured level for the step type, or
            the global default_level.
        """
        return self.validation.level_by_step_type.get(
            step_type.value,
            self.validation.default_level,
        )
