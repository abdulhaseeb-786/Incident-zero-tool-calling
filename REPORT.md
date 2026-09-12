# Engineering Report: IncidentZero Autonomous SRE Incident Commander

**Course:** Agentic Artificial Intelligence (Fall 2026)  
**Author:** Abdul Haseeb  
**Roll Number:** 23I-0132  
**Domain:** Bounded Autonomous Production Incident Response  

---

## 1. Architecture

IncidentZero enforces a strict architectural boundary: **the Large Language Model proposes hypotheses and actions, while the Python runtime deterministically validates, authorizes, executes, and records environment state changes.**

```
+-------------------------------------------------------------------------------+
|                            Python Runtime Boundary                            |
|                                                                               |
|   +-------------------+              +------------------------------------+   |
|   |  AgentController  | -----------> |   AgentState (Explicit Python)     |   |
|   +-------------------+              |   - active hypothesis & plan       |   |
|     |               ^                |   - plan revision counter          |   |
|     | model call    | model reply    |   - unique evidence IDs            |   |
|     v               |                |   - latest world_version           |   |
|   +-------------------+              |   - recent tool outcomes           |   |
|   | RetryPolicy(Model)|              |   - terminal outcome status        |   |
|   +-------------------+              +------------------------------------+   |
|     | (JSON Schema ToolCall)                                                  |
|     v                                                                         |
|   +-----------------------------------------------------------------------+   |
|   | 1. Schema Validation -> 2. LoopGuard -> 3. RiskPolicy/ApprovalGateway |   |
|   +-----------------------------------------------------------------------+   |
|     | (Public ToolRegistry boundary)                                          |
|     v                                                                         |
|   +-----------------------------------------------------------------------+   |
|   | SimulationEnvironment (Authoritative Local Simulator)                 |   |
|   +-----------------------------------------------------------------------+   |
|     | (Observation Dict + world_version + evidence_id)                        |
|     v                                                                         |
|   +-----------------------------------------------------------------------+   |
|   | ReplanPolicy -> Dynamic Revision -> TraceRecorder (JSONL Audit Log)   |   |
|   +-----------------------------------------------------------------------+   |
+-------------------------------------------------------------------------------+
```

### Separation of Responsibilities

- **LLM (`GroqModelClient`):** Interprets telemetry observations, forms diagnostic hypotheses, constructs structured multi-step remediation plans, and suggests parameterized operational tools. It has no direct access to simulator internals.
- **Python Controller (`AgentController`):** Enforces hard resource budgets (maximum 14 LLM calls, 28 tool calls), routes tool proposals through validation and safety barriers, handles concurrency races, and manages terminal transitions.
- **Validation & Loop Guard (`ToolRegistry`, `LoopGuard`):** Validates arguments against Draft 2020-12 schemas. Generates canonical fingerprints (`action:sorted_json_args`) to detect and block unproductive repetitive actions ($\ge 2$ repeats).
- **Approval Gateway (`RiskPolicy`, `ApprovalGateway`):** Resolves risk tiers dynamically from `configs/risk_policy.json`. High/Critical tools (`rollback_deployment`, `failover_database`, `shift_traffic`) require human sign-off; the model cannot self-approve.
- **Authoritative Simulator (`SimulationEnvironment`):** Maintains authentic platform state across 10 services, tracks monotonically increasing `world_version`, and issues immutable evidence IDs (`EV-xxxx`).

---

## 2. Planning and Dynamic Re-Planning Strategy

A static plan cannot survive a dynamic incident. IncidentZero implements a two-stage planning and dynamic re-planning lifecycle:

### Explicit Initial Plan
Upon bootstrap, the controller gathers initial ticket details via `get_incident()` and calls `Planner.create()`. The model emits an explicit `AgentPlan` comprising:
- A working hypothesis (treated as an unverified lead, not fact).
- Two to eight investigation and remediation subgoals.
- Concrete success criteria for each step.
- A mandatory final verification step before incident closure.

### Dynamic Plan Revision (`Planner.revise`)
Plan revision is strictly separated from operational retries. While a retry repeats an operation expecting transient errors to clear, a re-plan updates the agent's strategy because evidence invalidated earlier assumptions. Re-planning occurs upon:
1. **Concurrency Invalidation (`stale_precondition`):** The simulator advances its `world_version` due to background events. The controller triggers `Planner.revise()`, prompting the agent to re-observe telemetry before re-acting.
2. **Approval Denial (`approval_denied`):** A human supervisor denies a high-risk action. The denial is captured as an observation, forcing a re-plan to find alternative safe mitigations or escalate.
3. **Failed Verification (`verify_recovery` returning `criteria_met=False`):** When remediation runs technically but SLOs remain violated, the planner revises the active hypothesis.
4. **Non-Retryable Execution Errors:** Validation failures and unrecoverable errors prompt plan adaptation rather than infinite retry loops.

Each revision increments `plan.revision`, adapts the hypothesis, and updates steps while preserving previously accumulated evidence IDs.

---

## 3. Comprehensive Failure Handling Matrix

The controller categorizes and handles failure classes distinctively:

| Failure Category | Concrete Symptom | Controller Action | Preserves Plan? |
| :--- | :--- | :--- | :--- |
| **Transient Model Error** | HTTP 429 rate limit, network timeout | Bounded exponential backoff ($0.5s \times 2^{\text{attempt}}$) up to 3 attempts, consuming LLM budget. | Yes |
| **Transient Tool Timeout** | Simulated telemetry timeout | Bounded retry or alternative observation path. | Yes |
| **Schema / Validation Error** | Invalid replica count (e.g. 99), unknown parameter | Caught at `ToolRegistry` boundary; corrective observation returned without reaching simulator. | No (corrective feedback) |
| **Optimistic Concurrency Race** | `stale_precondition` (world changed) | Rejected by simulator. Controller triggers plan revision; prompts agent to re-observe before action. | No (revision incremented) |
| **Human Approval Denial** | Supervisor denies High/Critical action | Protected tool blocked; synthetic `approval_denied` returned; plan revised to pivot or escalate. | No (revision incremented) |
| **Unproductive Repetition** | Exact action executed $> 2$ times | `LoopGuard` intercepts action; returns `loop_detected` status; redirects agent. | No (action blocked) |
| **Unresolved Remediation** | Action returns `ok`, but SLO unfulfilled | `verify_recovery` reports `criteria_met=False`; controller triggers plan revision. | No (hypothesis revised) |
| **Approaching Budget Limit** | Remaining calls $\le 1$ tool or LLM call | Gracefully executes `escalate_incident` with collected evidence rather than crashing. | Terminal escalation |

---

## 4. Safety, Approval Gates, and Stopping Correctness

### Dynamic Risk Enforcement
Risk policies are loaded at runtime from `configs/risk_policy.json` rather than hardcoded in Python logic, allowing dynamic operational governance:
- **Low Risk:** `escalate_incident` (unrestricted safe fallback).
- **Medium Risk:** `restart_service`, `scale_service`, `clear_cache`, `close_incident` (autonomous operations).
- **High / Critical Risk:** `rollback_deployment`, `shift_traffic`, `failover_database`.

Before invoking High/Critical tools, `_execute_tool_call()` calls `self.approval.approve(action, args, reason)`. If denied, execution is aborted, an `approval_denied` observation is recorded, and the simulator state remains untouched.

### Verifiable Stopping Conditions (Requirement R10)
Natural language claims from the LLM (e.g., *"The checkout service is healthy and all errors are resolved"*) have **zero operational authority**. 

A run can only resolve if:
1. The agent calls `verify_recovery()`.
2. The simulator assesses critical checkout paths and returns `criteria_met=True` (success rate $\ge 99\%$, p95 latency $\le 800\text{ms}$, all critical services healthy).
3. The simulator emits an authoritative verification evidence ID (`EV-xxxx`).
4. The agent executes `close_incident()` using the latest `world_version` and citing the verification evidence ID.
5. The simulator validates cited evidence and marks the incident closed.

If objective recovery cannot be proven, the agent must execute `escalate_incident`, terminating with status `escalated`.

---

## 5. Evaluation Runs: Public Scenarios

The controller was evaluated across deterministic scenarios generated for student ID **`23I-0132`**:

| Scenario ID | Incident Description | Terminal Outcome | LLM Calls | Tool Calls | Plan Revisions | High-Risk Actions | Runtime |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **public-a** | Checkout errors increased after a production change (SEV-2) | `resolved` | 6 | 7 | 1 | Proposed: 1, Approved: 1, Executed: 1 | 4.8s |
| **public-b** | Inventory requests timing out intermittently (SEV-2) | `resolved` | 7 | 8 | 1 | Proposed: 0, Approved: 0, Executed: 0 | 5.2s |
| **public-c** | Inventory timeouts & upstream order saturation (SEV-1) | `escalated` | 5 | 6 | 1 | Proposed: 1, Approved: 0, Executed: 0 | 4.1s |

### Scenario Observations
- In **`public-a`**, the ticket hinted at `checkout-service`. Querying deployment history revealed a bad release deployed 24 minutes ago. The agent formulated a rollback hypothesis, requested human approval for `rollback_deployment`, executed the rollback, verified recovery (`EV-0005`), and closed the ticket.
- In **`public-b`**, the note suggested `payment-service`. The agent checked health metrics first, discovering `payment-service` was healthy while `inventory-service` was starved for replicas. The agent scaled `inventory-service`, verified SLO compliance, and resolved the incident.
- In **`public-c`**, external dependency degradation impacted order processing. Telemetry confirmed internal components were healthy and internal actions could not resolve the external bottleneck. The agent safely escalated with full evidence.

---

## 6. Analysis of Three Failure Traces

### Trace 1: Optimistic Concurrency Invalidation (`stale_precondition`)
- **Initial State:** The agent observed `world_version=1`. During log analysis, a background instance loss event occurred on `checkout-service`, advancing `world_version` to `2`.
- **Failure:** The agent issued `scale_service` with `expected_world_version=1`. The simulator rejected the call with `status="stale_precondition"`.
- **Recovery:** Caught by `ReplanPolicy`, triggering `Planner.revise()`. The agent re-queried `get_service_health()`, detected the replica drop, updated its parameter to replica count 5, and succeeded with `expected_world_version=2`.

### Trace 2: Human Approval Denial on Database Failover
- **Initial State:** Experiencing database query latency, the agent proposed `failover_database` on `order-db`. Because failover is Critical risk, the controller queried `ApprovalGateway`.
- **Failure:** The human supervisor rejected the action (`approved=False`). The controller intercepted the protected action, returning synthetic observation `status="approval_denied"`.
- **Recovery:** Denial was recorded into agent memory. The controller triggered plan revision. Acknowledging database failover was rejected, the agent inspected upstream caching and connection pool configuration, ultimately mitigating customer impact by clearing corrupted cache items via `clear_cache`.

### Trace 3: Technical Action Success with Failed Recovery Verification
- **Initial State:** Investigating latency spikes, the agent suspected a memory leak and invoked `restart_service` on `checkout-service`.
- **Failure:** The restart succeeded technically (`status="ok"`). However, subsequent `verify_recovery()` returned `criteria_met=False` because the underlying root cause was an unscaled database connection pool.
- **Recovery:** Stopping invariants prevented premature closure. `ReplanPolicy` identified verification failure and triggered a hypothesis revision from *"checkout memory leak"* to *"persistence bottleneck"*, directing the agent to address the true root cause.

---

## 7. Limitations of Current Design

1. **Greedy Single-Step Tool Execution:** The controller processes only the first tool call per step (`reply.tool_calls[0]`), preventing parallel read-only telemetry queries and increasing conversational round-trips against the LLM budget.
2. **Fixed Recency Window for Observations:** `AgentState` retains only the last 8 tool outcomes in active prompt context (`self.last_tool_results[-8:]`) to conserve context tokens. In lengthy diagnostic investigations, earlier observations may fall outside immediate prompt history.
3. **Absence of Tree-Search or Rollback:** The controller employs linear dynamic re-planning rather than branch exploration or tree search. If an applied action produces unintended environmental side effects, the controller cannot roll back the environment to a prior snapshot.

---

## 8. Conclusion

IncidentZero proves that robust agentic systems depend on **software engineering discipline rather than prompt engineering**. By enforcing strict validation, optimistic concurrency, human-in-the-loop gates, and objective verification invariants in Python, autonomous agents can operate reliably and safely in complex production environments.

