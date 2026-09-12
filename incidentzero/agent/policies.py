from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from incidentzero.domain.models import RiskLevel


class RiskPolicy:
    def __init__(self, config_path: str | Path = "configs/risk_policy.json") -> None:
        self.mapping = json.loads(Path(config_path).read_text(encoding="utf-8"))

    def risk(self, tool_name: str) -> RiskLevel:
        return RiskLevel(self.mapping.get(tool_name, "critical"))

    def requires_human_approval(self, tool_name: str) -> bool:
        return self.risk(tool_name) in {RiskLevel.HIGH, RiskLevel.CRITICAL}


class ReplanPolicy:
    def should_replan(self, tool_result: dict[str, Any]) -> bool:
        """Determine if an execution result warrants revising the agent's plan.

        Re-planning is warranted when:
        - The world state changed under optimistic concurrency (stale_precondition).
        - A human supervisor explicitly denied approval for a high/critical action.
        - A non-retryable action or validation error occurred.
        - Objective recovery verification failed after an intended remediation action.
        Transient errors (e.g. transient timeouts) should be retried by the retry policy,
        not treated as a reason to discard the plan.
        """
        status = tool_result.get("status")
        retryable = tool_result.get("retryable", False)

        # Concurrency race: world changed after observation
        if status == "stale_precondition":
            return True

        # Safety gate: human supervisor denied high/critical action
        if status == "approval_denied":
            return True

        # Non-retryable failure in action or schema validation
        if status in {"error", "action_failed", "validation_error"} and not retryable:
            return True

        # Objective verification failed (remediation technically ran but customer impact persists)
        if tool_result.get("tool") == "verify_recovery":
            data = tool_result.get("data")
            if isinstance(data, dict) and data.get("criteria_met") is False:
                return True

        return False


class LoopGuard:
    def __init__(self, max_same_action_repeats: int = 2) -> None:
        self.max_same_action_repeats = max_same_action_repeats
        self._counts: dict[str, int] = {}

    def fingerprint(self, action_name: str, arguments: dict[str, Any]) -> str:
        """Create a deterministic, canonical fingerprint for an action and its arguments."""
        try:
            serialized_args = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        except Exception:
            serialized_args = str(sorted(arguments.items()))
        return f"{action_name}:{serialized_args}"

    def record(self, action_name: str, arguments: dict[str, Any]) -> bool:
        """Record an action execution and return True when the exact same action has repeated too often."""
        fp = self.fingerprint(action_name, arguments)
        self._counts[fp] = self._counts.get(fp, 0) + 1
        return self._counts[fp] > self.max_same_action_repeats

    def count(self, action_name: str, arguments: dict[str, Any]) -> int:
        """Get the current repetition count for a specific action fingerprint."""
        fp = self.fingerprint(action_name, arguments)
        return self._counts.get(fp, 0)

    def reset(self) -> None:
        """Reset repetition tracking when material world changes or plan revisions occur."""
        self._counts.clear()
