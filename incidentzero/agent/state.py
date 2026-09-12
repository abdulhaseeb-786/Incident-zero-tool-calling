from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from incidentzero.domain.models import AgentPlan


@dataclass
class AgentState:
    messages: list[dict[str, Any]] = field(default_factory=list)
    plan: AgentPlan | None = None
    evidence_ids: list[str] = field(default_factory=list)
    latest_world_version: int | None = None
    last_tool_results: list[dict[str, Any]] = field(default_factory=list)
    repeated_actions: dict[str, int] = field(default_factory=dict)
    status: str = "running"

    def observe_result(self, result: dict[str, Any]) -> None:
        """Process an execution observation to update authoritative Python state."""
        evidence = result.get("evidence_id")
        if evidence and evidence not in self.evidence_ids:
            self.evidence_ids.append(evidence)
        version = result.get("world_version")
        if isinstance(version, int):
            self.latest_world_version = version
        self.last_tool_results.append(result)
        self.last_tool_results = self.last_tool_results[-8:]

    def summary(self) -> str:
        """Produce a structured, evidence-grounded summary of current state for planning/replanning."""
        plan_summary = (
            f"Hypothesis: {self.plan.hypothesis} (rev {self.plan.revision})"
            if self.plan
            else "No active plan"
        )
        recent_outcomes = [
            f"{r.get('tool')}:{r.get('status')}"
            for r in self.last_tool_results[-4:]
        ]
        return (
            f"State[world_version={self.latest_world_version}, status={self.status}, "
            f"plan={plan_summary}, evidence_count={len(self.evidence_ids)}, "
            f"recent={recent_outcomes}]"
        )

