# IncidentZero Starter Repository

**Assignment 1 - Agentic Artificial Intelligence (Fall 2026)**  
**Domain:** bounded autonomous SRE / production-incident response  
**Mode:** individual assignment, plain Python + Groq SDK, no agent framework

This repository deliberately gives you a **working simulated production environment** and an **incomplete agent runtime**. Your job is not to build an API or a dashboard. Your job is to turn the baseline loop into a reliable agent that can observe, plan, act, verify, re-plan, recover from failures, respect human approval, and stop correctly under a strict budget.

## Student Submission Information

- **Student Name:** Abdul Haseeb
- **Roll Number:** 23I-0132
- **Course:** Agentic Artificial Intelligence (Fall 2026)

## Exact Setup and Run Instructions

### 1. Environment Setup

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt
```

Set up your `.env` configuration file:
```bash
copy .env.example .env   # Windows
# cp .env.example .env   # Linux/macOS
```
Ensure `GROQ_API_KEY` is set in `.env` or exported in your shell. **Never commit the API key.**

### 2. Run Verification & Unit Tests (Offline - No Quota Consumed)

```bash
# Infrastructure & Banned Framework Checks
pytest -q tests/public -m infrastructure
python scripts/check_banned_imports.py
python scripts/check_protected_integrity.py

# Full public test suite including all 11 student unit tests:
pytest -q tests/public
```

### 3. Generate Scenarios & Run Evaluated Scenarios

```bash
# Generate deterministic public scenarios:
python scripts/generate_student_scenario.py --student-id 23I-0132 --scenario public-a
python scripts/generate_student_scenario.py --student-id 23I-0132 --scenario public-b
python scripts/generate_student_scenario.py --student-id 23I-0132 --scenario public-c

# Live evaluation run (interactive console approval):
python -m incidentzero.cli run --student-id 23I-0132 --scenario public-a

# Automated non-interactive run (auto-approve high/critical actions):
python -m incidentzero.cli run --student-id 23I-0132 --scenario public-a --auto-approve
```


## Stable contract

Read `docs/CONTRACTS.md` before editing. Hidden grading assumes those public interfaces still exist. You may refactor internally, but do not delete or rename required public classes/functions.

## Protected areas

Do not modify the simulator to make scenarios easier. The grader uses clean copies and additional hidden scenarios. In particular, do not depend on private fields or anything named `_oracle`, `_root_cause`, or `_scenario_spec`.

The point is to build a robust **controller**, not to reverse-engineer the answer from the simulator source.
