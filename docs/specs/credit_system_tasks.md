# Credit System Task List

## Phase 1: Credit System (do now)

### Backend
- Fix credit settlement to use actual audio duration from synthesize output.
- Use session TTL for reservation expiry (no hardcoded TTL in credits module).
- Add ledger entries for reserve and release actions.
- Block synthesize unless a recent estimate exists for the session (guardrail).
- Clear credit estimate metadata after reservation attempt.

### LLM + Orchestration
- Update system prompt to require `estimate_credits` before `synthesize` and ask for explicit user confirmation.
- Ensure estimate results are stored per-session so the next call can reserve credits based on that estimate.

### Frontend
- Trigger a backend `/credits` call on sign-in to ensure trial credits are created.
- Keep the Credits header driven by Firestore updates.

## Phase 2: Waiting List (do after credits are done)
- Implement waiting list backend module and API.
- Add Firestore rules for `waiting_list/{uid}`.
- Build waiting list UI and CTA on credit exhaustion.
- Wire UI to backend endpoint.
