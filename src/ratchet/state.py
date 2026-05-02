"""Holds logical outputs (StepOutput) indexed by step_id.

Single-writer (plan_executor), so no asyncio lock is needed.
"""

from ratchet.plan.schema import StepOutput


class State:
    """In-memory store for structured step outputs.

    State holds logical outputs between steps. Physical state (the repo
    filesystem) is shared via cwd=repo_path across all executors.
    """

    def __init__(self) -> None:
        """Initialise an empty State."""
        self._outputs: dict[str, StepOutput] = {}

    def record(self, step_id: str, output: StepOutput) -> None:
        """Store the output of a completed step.

        Args:
            step_id: The step that produced the output.
            output: The executor's structured output.
        """
        self._outputs[step_id] = output

    def resolve(self, step_ids: list[str]) -> dict[str, StepOutput]:
        """Retrieve outputs for a list of step ids.

        Args:
            step_ids: Step ids to resolve.

        Returns:
            Dict mapping each found step_id to its StepOutput.
            Missing ids are silently omitted.
        """
        return {sid: self._outputs[sid] for sid in step_ids if sid in self._outputs}

    def get(self, step_id: str) -> StepOutput | None:
        """Retrieve the output of a single step.

        Args:
            step_id: The step to look up.

        Returns:
            The StepOutput if recorded, else None.
        """
        return self._outputs.get(step_id)
