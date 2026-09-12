from __future__ import annotations

import json
from typing import Any

from incidentzero.approval.gateway import ApprovalGateway
from incidentzero.domain.models import AgentOutcome, ModelReply, ToolCall
from incidentzero.model.base import ModelClient
from incidentzero.telemetry.budget import BudgetExceeded, BudgetManager
from incidentzero.telemetry.trace import TraceRecorder
from incidentzero.tools.registry import ToolRegistry

from .planner import Planner
from .policies import LoopGuard, ReplanPolicy, RiskPolicy
from .prompts import SYSTEM_PROMPT
from .recovery import RetryPolicy
from .state import AgentState


class AgentController:
    """Baseline controller derived from the class 'first agent' loop.

    It intentionally lacks production reliability. Your assignment is to evolve this
    controller rather than replacing it with an agent framework.
    """

    def __init__(
        self,
        model: ModelClient,
        tools: ToolRegistry,
        approval: ApprovalGateway,
        budget: BudgetManager,
        trace: TraceRecorder,
    ) -> None:
        self.model = model
        self.tools = tools
        self.approval = approval
        self.budget = budget
        self.trace = trace
        self.state = AgentState()
        self.planner = Planner(model)
        self.risk = RiskPolicy()
        self.replan_policy = ReplanPolicy()
        self.loop_guard = LoopGuard()
        self.retry_policy = RetryPolicy()

    def _model_decide(self) -> ModelReply:
        """Call model with bounded retries and budget accounting for transient errors."""
        def _invoke() -> ModelReply:
            self.budget.consume_llm()
            return self.model.decide(self.state.messages, self.tools.groq_tools)

        return self.retry_policy.call_model(_invoke)

    def _execute_tool_call(self, call: ToolCall) -> dict[str, Any]:
        """Validate, approve if needed, execute, trace, and return one observation.

        Enforces:
        - Tool call budget consumption.
        - Schema & argument validation boundary.
        - Loop detection (blocking unproductive repetition).
        - Human approval gateway for High/Critical actions.
        - Public execution via ToolRegistry.
        - Comprehensive audit tracing.
        """
        self.budget.consume_tool()

        # 1. Validation boundary
        ok, error = self.tools.validate(call.name, call.arguments)
        if not ok:
            result = {
                "status": "validation_error",
                "tool": call.name,
                "world_version": self.state.latest_world_version or self.tools.environment.world_version,
                "evidence_id": None,
                "data": None,
                "retryable": False,
                "message": error,
            }
            self.trace.record("validation_error", {"call": {"name": call.name, "arguments": call.arguments}, "error": error})
            return result

        # 2. Loop detection check
        if self.loop_guard.record(call.name, call.arguments):
            result = {
                "status": "loop_detected",
                "tool": call.name,
                "world_version": self.state.latest_world_version or self.tools.environment.world_version,
                "evidence_id": None,
                "data": None,
                "retryable": False,
                "message": f"Action {call.name} repeated beyond allowed threshold. Blocked to prevent loop thrashing.",
            }
            self.trace.record("loop_blocked", {"call": {"name": call.name, "arguments": call.arguments}})
            return result

        # 3. Human Approval Gate for High/Critical actions
        if self.risk.requires_human_approval(call.name):
            justification = str(call.arguments.get("reason", f"Execution of {call.name}"))
            approved = self.approval.approve(call.name, call.arguments, justification)
            self.trace.record("approval_request", {
                "tool": call.name,
                "arguments": call.arguments,
                "justification": justification,
                "approved": approved,
            })
            if not approved:
                result = {
                    "status": "approval_denied",
                    "tool": call.name,
                    "world_version": self.state.latest_world_version or self.tools.environment.world_version,
                    "evidence_id": None,
                    "data": None,
                    "retryable": False,
                    "message": f"Human supervisor denied approval for action {call.name}.",
                }
                self.trace.record("approval_denied", {"tool": call.name, "arguments": call.arguments})
                return result

        # 4. Authoritative execution in simulated environment
        result = self.tools.execute(call.name, call.arguments)
        self.trace.record("tool_result", {"call": {"name": call.name, "arguments": call.arguments}, "result": result})
        return result

    def _append_assistant(self, reply: ModelReply) -> None:
        msg: dict[str, Any] = {"role": "assistant", "content": reply.content}
        if reply.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in reply.tool_calls
            ]
        self.state.messages.append(msg)

    def _append_tool_result(self, call: ToolCall, result: dict[str, Any]) -> None:
        self.state.messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(result, ensure_ascii=False),
        })

    def run(self) -> AgentOutcome:
        self.state.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Investigate the active production incident, mitigate it safely, verify recovery, then close it; otherwise escalate with evidence."},
        ]
        try:
            # Bootstrap with one real observation so the plan is grounded in environment evidence.
            self.budget.consume_tool()
            incident = self.tools.execute("get_incident", {})
            self.state.observe_result(incident)
            self.trace.record("bootstrap_incident", incident)
            self.state.messages.append({"role": "system", "content": f"Current incident evidence: {json.dumps(incident)}"})

            self.budget.consume_llm()
            self.state.plan = self.planner.create(incident)
            self.trace.record("plan_created", {"plan": str(self.state.plan)})

            while self.budget.remaining_llm > 0 and self.budget.remaining_tools > 0:
                # Proactive resource safety: safely escalate if budget is almost depleted
                if self.budget.remaining_llm <= 1 or self.budget.remaining_tools <= 1:
                    if self.budget.remaining_tools >= 1:
                        self.budget.consume_tool()
                        esc_res = self.tools.execute("escalate_incident", {
                            "reason": "Autonomous budget nearly exhausted; escalating to on-call engineer with collected evidence.",
                            "evidence_ids": list(self.state.evidence_ids),
                        })
                        self.state.observe_result(esc_res)
                        self.state.status = "escalated"
                        return AgentOutcome(
                            status="escalated",
                            summary="Safely escalated before resource budget exhausted.",
                            llm_calls=self.budget.llm_calls,
                            tool_calls=self.budget.tool_calls,
                            final_world_version=self.state.latest_world_version,
                            evidence_ids=self.state.evidence_ids,
                            trace_path=str(self.trace.path),
                        )
                    break

                reply = self._model_decide()
                self._append_assistant(reply)
                self.trace.record("model_reply", {
                    "content": reply.content,
                    "tool_calls": [
                        c.__dict__ if hasattr(c, "__dict__") else {"name": c.name, "arguments": c.arguments}
                        for c in reply.tool_calls
                    ],
                })

                if not reply.tool_calls:
                    self.state.status = "failed"
                    return AgentOutcome(
                        status="failed",
                        summary="Model stopped without a tool call; natural language claims cannot close incidents.",
                        llm_calls=self.budget.llm_calls,
                        tool_calls=self.budget.tool_calls,
                        final_world_version=self.state.latest_world_version,
                        evidence_ids=self.state.evidence_ids,
                        trace_path=str(self.trace.path),
                    )

                # Starter executes one action at a time.
                call = reply.tool_calls[0]
                result = self._execute_tool_call(call)
                self.state.observe_result(result)
                self._append_tool_result(call, result)

                # Verifiable Stopping Conditions (Requirement R10)
                if call.name == "close_incident" and result.get("status") == "ok":
                    self.state.status = "resolved"
                    return AgentOutcome(
                        status="resolved",
                        summary="Incident verified and closed with simulator evidence.",
                        llm_calls=self.budget.llm_calls,
                        tool_calls=self.budget.tool_calls,
                        final_world_version=self.state.latest_world_version,
                        evidence_ids=self.state.evidence_ids,
                        trace_path=str(self.trace.path),
                    )
                if call.name == "escalate_incident" and result.get("status") == "ok":
                    self.state.status = "escalated"
                    return AgentOutcome(
                        status="escalated",
                        summary="Incident safely escalated with gathered evidence.",
                        llm_calls=self.budget.llm_calls,
                        tool_calls=self.budget.tool_calls,
                        final_world_version=self.state.latest_world_version,
                        evidence_ids=self.state.evidence_ids,
                        trace_path=str(self.trace.path),
                    )

                # Dynamic Re-planning (Task A / Task F / R4, R5, R6)
                if self.replan_policy.should_replan(result):
                    if self.state.plan and self.budget.remaining_llm > 1:
                        self.budget.consume_llm()
                        self.state.plan = self.planner.revise(
                            self.state.plan,
                            result,
                            state_summary=self.state.summary(),
                        )
                        self.trace.record("plan_revised", {
                            "plan": str(self.state.plan),
                            "trigger": result,
                            "revision": self.state.plan.revision,
                        })
                        self.state.messages.append({
                            "role": "system",
                            "content": (
                                f"Dynamic Re-plan Triggered (rev {self.state.plan.revision}): "
                                f"Trigger={result.get('status')}. Updated hypothesis: {self.state.plan.hypothesis}. "
                                "Re-observe relevant state or choose a safe alternative."
                            ),
                        })

            self.state.status = "budget_exhausted"
            return AgentOutcome(
                status="budget_exhausted",
                summary="Agent budget exhausted before safe termination.",
                llm_calls=self.budget.llm_calls,
                tool_calls=self.budget.tool_calls,
                final_world_version=self.state.latest_world_version,
                evidence_ids=self.state.evidence_ids,
                trace_path=str(self.trace.path),
            )
        except BudgetExceeded as exc:
            self.state.status = "budget_exhausted"
            return AgentOutcome(
                status="budget_exhausted",
                summary=str(exc),
                llm_calls=self.budget.llm_calls,
                tool_calls=self.budget.tool_calls,
                final_world_version=self.state.latest_world_version,
                evidence_ids=self.state.evidence_ids,
                trace_path=str(self.trace.path),
            )
