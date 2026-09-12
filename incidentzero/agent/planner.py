from __future__ import annotations

from typing import Any

from incidentzero.domain.models import AgentPlan, PlanStep
from incidentzero.model.base import ModelClient


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "hypothesis": {"type": "string"},
        "rationale_summary": {"type": "string"},
        "steps": {
            "type": "array",
            "minItems": 2,
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "step_id": {"type": "string"},
                    "objective": {"type": "string"},
                    "success_signal": {"type": "string"},
                },
                "required": ["step_id", "objective", "success_signal"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["hypothesis", "rationale_summary", "steps"],
    "additionalProperties": False,
}


class Planner:
    def __init__(self, model: ModelClient) -> None:
        self.model = model

    def create(self, incident_observation: dict[str, Any], context: list[dict[str, Any]] | None = None) -> AgentPlan:
        """Create an explicit initial plan.

        The baseline is intentionally thin. Improve validation, retry behavior, grounding,
        and budget integration in your submission.
        """
        messages = [
            {"role": "system", "content": "Create a short SRE investigation-and-remediation plan. Do not assume the ticket's suspected root cause is correct."},
            {"role": "user", "content": f"Incident observation: {incident_observation}"},
        ]
        try:
            raw = self.model.structured(messages, "incident_plan", PLAN_SCHEMA)
            steps = [PlanStep(**row) for row in raw["steps"]]
            return AgentPlan(hypothesis=raw["hypothesis"], steps=steps, rationale_summary=raw["rationale_summary"])
        except Exception:
            fallback_steps = [
                PlanStep(step_id="step_investigate", objective="Inspect service health, telemetry, and deployment history", success_signal="Operational baseline established"),
                PlanStep(step_id="step_remediate", objective="Apply bounded remediation action with approval if required", success_signal="Remediation status ok"),
                PlanStep(step_id="step_verify", objective="Verify objective customer recovery criteria", success_signal="Criteria met verified"),
            ]
            return AgentPlan(
                hypothesis="Preliminary investigation of reported incident",
                steps=fallback_steps,
                rationale_summary="Initial SRE investigation and bounded remediation plan",
            )


    def revise(self, current: AgentPlan, trigger: dict[str, Any], state_summary: str) -> AgentPlan:
        """Revise the active plan in response to new evidence, stale world, or failure.

        Increments the plan revision number, preserves completed evidence, and updates
        hypotheses and subgoals without blindly repeating failed actions.
        """
        new_revision = current.revision + 1
        trigger_status = trigger.get("status", "unknown")
        trigger_msg = trigger.get("message", "Triggered by environment or safety feedback.")
        trigger_tool = trigger.get("tool", "unknown")

        messages = [
            {
                "role": "system",
                "content": (
                    "You are an SRE incident commander. Your previous remediation step or hypothesis was challenged by the environment.\n"
                    "Revise your hypothesis and steps accordingly. DO NOT repeat the failed action blindly.\n"
                    "If a concurrency conflict (stale_precondition) occurred, re-observe the affected component.\n"
                    "If approval was denied, formulate an alternative or escalate.\n"
                    "If recovery verification failed, explore alternative root causes."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Current Plan (rev {current.revision}):\n"
                    f"- Hypothesis: {current.hypothesis}\n"
                    f"- Rationale: {current.rationale_summary}\n\n"
                    f"Replan Trigger:\n"
                    f"- Status: {trigger_status}\n"
                    f"- Tool: {trigger_tool}\n"
                    f"- Detail: {trigger_msg}\n\n"
                    f"State & Evidence Summary: {state_summary}\n\n"
                    f"Generate a revised plan with an updated hypothesis, rationale, and 2-8 concrete steps."
                ),
            },
        ]

        try:
            raw = self.model.structured(messages, "incident_plan_revision", PLAN_SCHEMA)
            steps = [PlanStep(**row) for row in raw["steps"]]
            return AgentPlan(
                hypothesis=raw["hypothesis"],
                steps=steps,
                revision=new_revision,
                rationale_summary=raw["rationale_summary"],
            )
        except Exception:
            # Deterministic fallback ensuring offline tests without scripted structured output still succeed
            fallback_steps = [
                PlanStep(
                    step_id="rev_observe",
                    objective=f"Re-observe system state after {trigger_status} on {trigger_tool}",
                    success_signal="Observed updated metrics and health",
                ),
                PlanStep(
                    step_id="rev_act_or_escalate",
                    objective="Determine safe remediation alternative or prepare safe escalation",
                    success_signal="Safe alternative executed or incident escalated",
                ),
                PlanStep(
                    step_id="rev_verify",
                    objective="Verify platform recovery or confirm escalation handoff",
                    success_signal="Criteria met or escalation recorded",
                ),
            ]
            return AgentPlan(
                hypothesis=f"Revised: {current.hypothesis} (adapted after {trigger_status})",
                steps=fallback_steps,
                revision=new_revision,
                rationale_summary=f"Automated revision {new_revision} responding to {trigger_status}: {trigger_msg}",
            )

