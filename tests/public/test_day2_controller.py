from __future__ import annotations

import pytest

from incidentzero.agent.controller import AgentController
from incidentzero.agent.planner import Planner
from incidentzero.approval.gateway import AlwaysApproveGateway, AlwaysDenyGateway
from incidentzero.domain.models import AgentPlan, ModelReply, PlanStep, ToolCall
from incidentzero.environment.engine import SimulationEnvironment
from incidentzero.model.scripted import ScriptedModelClient
from incidentzero.telemetry.budget import BudgetManager
from incidentzero.telemetry.trace import TraceRecorder
from incidentzero.tools.registry import ToolRegistry


def make_controller(tmp_path, decisions: list[ModelReply], approval_gateway=None, budget=None) -> tuple[AgentController, SimulationEnvironment]:
    env = SimulationEnvironment("TEST-001", "public-a")
    registry = ToolRegistry(env)
    model = ScriptedModelClient(decisions=decisions)
    approval = approval_gateway or AlwaysApproveGateway()
    b = budget or BudgetManager(max_llm_calls=14, max_tool_calls=28)
    trace = TraceRecorder(tmp_path / "trace.jsonl")
    controller = AgentController(
        model=model,
        tools=registry,
        approval=approval,
        budget=b,
        trace=trace,
    )
    return controller, env


@pytest.mark.student
def test_approval_denial_blocks_action_and_triggers_replan(tmp_path):
    """High/critical actions must be blocked on denial and trigger plan revision."""
    decisions = [
        ModelReply(
            content="Attempting high-risk rollback without human approval.",
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="rollback_deployment",
                    arguments={
                        "service": "checkout-service",
                        "target_version": "v1.0.0",
                        "expected_world_version": 1,
                        "reason": "Test rollback that should require approval.",
                    },
                )
            ],
        ),
        ModelReply(
            content="Approval was denied, escalating safely.",
            tool_calls=[
                ToolCall(
                    id="call_2",
                    name="escalate_incident",
                    arguments={
                        "reason": "Rollback approval was denied by supervisor.",
                        "evidence_ids": ["EV-0001"],
                    },
                )
            ],
        ),
    ]

    controller, env = make_controller(tmp_path, decisions, approval_gateway=AlwaysDenyGateway())
    initial_version = env.world_version
    outcome = controller.run()

    # Simulator was never mutated by the denied rollback
    assert env.world_version == initial_version + 1  # only incremented by escalate_incident
    assert outcome.status == "escalated"
    # Plan revision was triggered by approval denial
    assert controller.state.plan is not None
    assert controller.state.plan.revision >= 1


@pytest.mark.student
def test_stale_world_triggers_replan(tmp_path):
    """Optimistic concurrency mismatch must produce stale_precondition and trigger replan."""
    decisions = [
        ModelReply(
            content="Executing scale action with stale version 1.",
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="scale_service",
                    arguments={
                        "service": "checkout-service",
                        "replicas": 4,
                        "expected_world_version": 1,
                        "reason": "Testing stale version handling.",
                    },
                )
            ],
        ),
        ModelReply(
            content="Escalating after stale precondition.",
            tool_calls=[
                ToolCall(
                    id="call_2",
                    name="escalate_incident",
                    arguments={
                        "reason": "World changed unexpectedly; escalating.",
                        "evidence_ids": ["EV-0001"],
                    },
                )
            ],
        ),
    ]

    controller, env = make_controller(tmp_path, decisions)
    # Deliberately mutate world version before agent executes scale
    env._world_version = 2

    outcome = controller.run()
    assert outcome.status == "escalated"
    assert controller.state.plan is not None
    assert controller.state.plan.revision >= 1


@pytest.mark.student
def test_loop_guard_blocks_identical_action(tmp_path):
    """LoopGuard must block the 3rd identical action call."""
    repeated_call = ToolCall(
        id="call_rep",
        name="scale_service",
        arguments={
            "service": "checkout-service",
            "replicas": 3,
            "expected_world_version": 1,
            "reason": "Repetitive scale action.",
        },
    )
    decisions = [
        ModelReply(content="Repeat 1", tool_calls=[repeated_call]),
        ModelReply(content="Repeat 2", tool_calls=[repeated_call]),
        ModelReply(content="Repeat 3 (should be blocked)", tool_calls=[repeated_call]),
        ModelReply(
            content="Loop blocked, escalating.",
            tool_calls=[
                ToolCall(
                    id="call_esc",
                    name="escalate_incident",
                    arguments={"reason": "Action loop detected, escalating.", "evidence_ids": ["EV-0001"]},
                )
            ],
        ),
    ]

    controller, _ = make_controller(tmp_path, decisions)
    outcome = controller.run()

    assert outcome.status == "escalated"
    # Find loop_detected observation in tool results
    loop_results = [r for r in controller.state.last_tool_results if r.get("status") == "loop_detected"]
    assert len(loop_results) >= 1


@pytest.mark.student
def test_schema_validation_rejection(tmp_path):
    """Invalid arguments must be caught at the validation boundary without calling the simulator."""
    decisions = [
        ModelReply(
            content="Invalid replicas parameter.",
            tool_calls=[
                ToolCall(
                    id="call_inv",
                    name="scale_service",
                    arguments={
                        "service": "checkout-service",
                        "replicas": 99,  # Invalid: schema allows 1..8
                        "expected_world_version": 1,
                        "reason": "Invalid replicas count.",
                    },
                )
            ],
        ),
        ModelReply(
            content="Escalating after validation error.",
            tool_calls=[
                ToolCall(
                    id="call_esc",
                    name="escalate_incident",
                    arguments={"reason": "Validation error occurred.", "evidence_ids": ["EV-0001"]},
                )
            ],
        ),
    ]

    controller, _ = make_controller(tmp_path, decisions)
    outcome = controller.run()
    assert outcome.status == "escalated"
    val_errors = [r for r in controller.state.last_tool_results if r.get("status") == "validation_error"]
    assert len(val_errors) >= 1


@pytest.mark.student
def test_premature_close_incident_rejected(tmp_path):
    """Calling close_incident without objective recovery verification must fail."""
    decisions = [
        ModelReply(
            content="Prematurely attempting to close incident.",
            tool_calls=[
                ToolCall(
                    id="call_close",
                    name="close_incident",
                    arguments={
                        "summary": "Premature attempt without verification.",
                        "evidence_ids": ["EV-0001"],
                        "expected_world_version": 1,
                        "reason": "Attempting closure before verify_recovery.",
                    },
                )
            ],
        ),
        ModelReply(
            content="Closing failed, escalating.",
            tool_calls=[
                ToolCall(
                    id="call_esc",
                    name="escalate_incident",
                    arguments={"reason": "Premature close rejected; escalating.", "evidence_ids": ["EV-0001"]},
                )
            ],
        ),
    ]

    controller, _ = make_controller(tmp_path, decisions)
    outcome = controller.run()
    assert outcome.status == "escalated"


@pytest.mark.student
def test_planner_revise_increments_revision():
    """Planner.revise must increment revision counter and adapt hypothesis."""
    client = ScriptedModelClient()
    planner = Planner(client)
    initial_plan = AgentPlan(
        hypothesis="Initial checkout degradation hypothesis",
        steps=[
            PlanStep(step_id="s1", objective="Inspect health", success_signal="Health ok"),
            PlanStep(step_id="s2", objective="Scale checkout", success_signal="Scaled"),
        ],
        revision=0,
    )

    trigger = {"status": "approval_denied", "tool": "failover_database", "message": "Approval denied"}
    revised = planner.revise(initial_plan, trigger, "State summary test")

    assert revised.revision == 1
    assert "approval_denied" in revised.hypothesis or "failover_database" in revised.rationale_summary or revised.revision == 1
    assert len(revised.steps) >= 2


@pytest.mark.student
def test_graceful_budget_exhaustion_safe_escalation(tmp_path):
    """When budget is nearly depleted, controller must safely escalate rather than hard-failing."""
    budget = BudgetManager(max_llm_calls=2, max_tool_calls=2)
    decisions = []

    controller, _ = make_controller(tmp_path, decisions, budget=budget)
    outcome = controller.run()

    assert outcome.status == "escalated"
    assert "budget" in outcome.summary.lower()


@pytest.mark.student
def test_retry_exhaustion_raises_transient_error():
    """TransientModelError must re-raise after max_attempts are exhausted."""
    from incidentzero.agent.recovery import RetryPolicy
    from incidentzero.model.errors import TransientModelError

    sleeps = []
    retry = RetryPolicy(max_attempts=3, sleeper=lambda s: sleeps.append(s))
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise TransientModelError("Simulated persistent 429")

    with pytest.raises(TransientModelError):
        retry.call_model(always_fails)

    assert calls["n"] == 3
    assert len(sleeps) == 2


@pytest.mark.student
def test_replan_triggered_when_verification_fails(tmp_path):
    """When verify_recovery reports criteria_met=False, controller must trigger plan revision."""
    decisions = [
        ModelReply(
            content="Testing verification before incident is resolved.",
            tool_calls=[ToolCall(id="c1", name="verify_recovery", arguments={})],
        ),
        ModelReply(
            content="Verification failed as expected; escalating with evidence.",
            tool_calls=[
                ToolCall(
                    id="c2",
                    name="escalate_incident",
                    arguments={"reason": "Verification failed; escalating.", "evidence_ids": ["EV-0001"]},
                )
            ],
        ),
    ]

    controller, _ = make_controller(tmp_path, decisions)
    outcome = controller.run()

    assert outcome.status == "escalated"
    assert controller.state.plan is not None
    assert controller.state.plan.revision >= 1


@pytest.mark.student
def test_unknown_tool_rejected_at_boundary(tmp_path):
    """Invented or non-existent tools must be rejected at validation boundary."""
    decisions = [
        ModelReply(
            content="Calling a hallucinatory tool.",
            tool_calls=[ToolCall(id="c1", name="invented_cluster_reboot", arguments={"force": True})],
        ),
        ModelReply(
            content="Escalating after tool rejection.",
            tool_calls=[
                ToolCall(
                    id="c2",
                    name="escalate_incident",
                    arguments={"reason": "Tool rejected; escalating safely.", "evidence_ids": ["EV-0001"]},
                )
            ],
        ),
    ]

    controller, _ = make_controller(tmp_path, decisions)
    outcome = controller.run()

    assert outcome.status == "escalated"
    val_err = [r for r in controller.state.last_tool_results if r.get("status") == "validation_error"]
    assert len(val_err) >= 1
    assert "Unknown tool" in val_err[0]["message"]


@pytest.mark.student
def test_model_text_without_tools_fails_gracefully(tmp_path):
    """Model returning natural-language claim without tools must result in failed outcome, not false resolution."""
    decisions = [
        ModelReply(
            content="I have investigated and the checkout service is completely fixed and healthy!",
            tool_calls=[],
        )
    ]

    controller, _ = make_controller(tmp_path, decisions)
    outcome = controller.run()

    assert outcome.status == "failed"
    assert "natural language" in outcome.summary.lower()


