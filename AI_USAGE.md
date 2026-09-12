# AI Assistance Declaration

Name: Abdul Haseeb  
Roll number: 23I-0132  

## Tools used
Antigravity (Google DeepMind / Gemini), Groq API console.

## What I used them for
I utilized AI assistance for architectural pair programming, implementing the Python runtime control loop, designing canonical argument serialization for action fingerprinting, establishing bounded exponential backoff recovery, structuring dynamic plan revisions, and developing offline deterministic unit tests using `ScriptedModelClient`.

## Two suggestions I rejected or changed
1. **Rejection of Pre-packaged Agent Frameworks:** Initial typical recommendations for multi-step agentic workflows rely on frameworks such as LangChain, LangGraph, or CrewAI. I strictly rejected the inclusion of external orchestration libraries to comply with the assignment contract and built a lightweight, framework-free Python state machine where the LLM only proposes actions while Python authoritatively validates, authorizes, and executes them.
2. **Refining LoopGuard Reset Semantics:** An early proposal suggested resetting the loop counter whenever a `stale_precondition` was encountered. I modified this behavior: a stale precondition means the proposed action failed without altering the environment. Resetting loop counts upon failure allowed repetitive thrashing; maintaining repetition counts across failed actions ensures the agent is redirected to observe and re-plan rather than blindly retrying.

## One AI-generated or AI-assisted bug I personally diagnosed
- **Symptom:** During offline testing (`test_loop_guard_blocks_identical_action`), the 3rd repeated identical call to `scale_service` was executed rather than being blocked with a `loop_detected` status.
- **Cause:** In `AgentController.run()`, encountering `stale_precondition` triggered an immediate `self.loop_guard.reset()`. Because the 1st scale call succeeded and advanced the simulator's `world_version` from 1 to 2, the 2nd identical call (with version 1) returned `stale_precondition`, resetting the frequency map to zero. The 3rd identical call was therefore registered with a repetition count of 1 rather than 3.
- **Fix:** Removed the premature loop guard reset on failed/stale actions in `controller.py`. Repetition counts now persist across rejected attempts so the loop threshold is strictly enforced.

## Code ownership statement
I can explain every submitted component, its failure behavior, and the trade-offs I chose. I understand that the TA may ask me to modify the code during viva.

Signature / typed name: Abdul Haseeb

