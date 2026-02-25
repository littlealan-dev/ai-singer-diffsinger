# SIG-6 Task List: Verse Lock + Preprocess Restart Contract

## Goal
- Prevent derived-part synthesis failures caused by verse-triggered reparse after preprocess.
- Make verse selection explicit and stable across `parse -> preprocess -> synthesize`.
- Enforce deterministic restart behavior when user changes verse after preprocess.

## Background
- Current orchestrator reparses whenever `verse_number` is present in synth args.
- Reparse can drop derived mapping/transform context, causing synth precheck failures even when LLM selected the correct derived target.
- Multi-verse handling must happen before preprocess because preprocess propagates verse-specific lyrics.

## Product/Workflow Policy
- Verse must be resolved before preprocess when `available_verses > 1`.
- After preprocess succeeds, selected verse is considered locked for that derived score.
- If user changes verse after preprocess, workflow must restart from parse and preprocess.

## Scope
- Backend parse/preprocess/synthesize contract.
- Orchestrator decision and guard logic.
- LLM prompt workflow guidance.
- Integration and e2e tests.

## Non-Goals
- Rewriting parser internals for global verse metadata in source MusicXML.
- Supporting multi-verse mixed output in one derived score.

## Workstream A: Persist Explicit Verse State
- [ ] Add `selected_verse_number` to parsed score payload (top-level score field).
- [ ] Ensure value is deterministic:
  - If user provided `verse_number`, persist that value.
  - Else persist parser effective default verse (first available verse).
- [ ] Add/verify session snapshot includes current score with `selected_verse_number`.
- [ ] Add preprocess metadata marker in score/session context:
  - `preprocessed_for_verse_number`
  - `preprocessed_for_score_fingerprint`

## Workstream B: Orchestrator Verse-Change Rules
- [ ] Replace unconditional synth reparse-on-verse behavior with explicit checks.
- [ ] Implement selection matching logic:
  - If requested `verse_number` equals persisted `selected_verse_number`, do not reparse.
  - If different, trigger verse-change flow.
- [ ] Implement verse-change flow behavior by stage:
  - Before preprocess: reparse only.
  - After preprocess/review pending: return `action_required` to restart parse->preprocess.
  - After prior synthesis on derived score: same restart requirement.
- [ ] Clear stale derived state on verse change:
  - review-pending marker
  - preprocess mapping context
  - transform cache references tied to old verse selection

## Workstream C: Synthesize Contract Hardening
- [ ] Add synth guard for incompatible verse/derived context.
- [ ] If synth request conflicts with verse-lock/preprocess context, return typed `action_required` (no silent fallback).
- [ ] Use explicit reason codes:
  - `verse_change_requires_reparse`
  - `verse_change_requires_repreprocess`
  - `derived_context_invalid_for_requested_verse`
- [ ] Keep non-audio synth results surfaced as actionable errors (not generic crashes).

## Workstream D: LLM Prompt & Tool Usage Rules
- [ ] Update `src/backend/config/system_prompt.txt`:
  - Verse selection must be confirmed before preprocess for multi-verse scores.
  - Do not include `verse_number` in synth if it matches persisted selected verse.
  - If user changes verse post-preprocess, restart parse->preprocess; do not synthesize immediately.
- [ ] Keep plan-construction guidance in `system_prompt_lessons.txt` separate from workflow rules.
- [ ] Add concise examples of correct/incorrect verse transitions.

## Workstream E: Tool Output/Diagnostics for Debugging
- [ ] Ensure preprocess/synth action-required payloads include diagnostics fields helpful for LLM repair:
  - `selected_verse_number`
  - `requested_verse_number`
  - `preprocessed_for_verse_number`
  - `preprocessed_score_detected`
- [ ] Keep mapping context visible in prompt (`preprocess_mapping_context`) and verify in logs.

## Workstream F: Tests
- [ ] Unit tests:
  - selection match logic with/without verse changes
  - synth guard reason code coverage
- [ ] Integration tests:
  - multi-verse score: choose verse before preprocess, preprocess, proceed synth (no reparse)
  - same verse passed in synth should not trigger reparse
  - verse changed after preprocess returns restart-required action
- [ ] Real-LLM e2e test updates (`tests/test_backend_e2e_gemini.py`):
  - assert prompt context includes selected/preprocessed verse state
  - assert no regression in derived target selection after preprocess

## Acceptance Criteria
- [ ] Synth no longer reparses solely because `verse_number` is present when verse is unchanged.
- [ ] Derived mapping remains valid from preprocess through synth in happy path.
- [ ] Changing verse after preprocess consistently yields restart-required action, not partial execution.
- [ ] LLM receives enough typed context to explain next action without guessing.
- [ ] Existing single-verse workflows remain unaffected.

## Risks / Notes
- Legacy clients that always send `verse_number` on synth may rely on old behavior.
- Score payload schema change (`selected_verse_number`) requires docs and prompt sync.
- Restart policy must be reflected clearly in UI copy once score preview refresh flow lands.

## Suggested Rollout
1. Backend guards + persisted verse fields.
2. Prompt updates.
3. Integration/e2e verification.
4. Enable by default in production workflow.
