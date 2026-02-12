# SIG-6 Implementation Checklist (No Human Intervention Until Final UAT)

## Execution Mode
This checklist assumes:
1. No human review between phases.
2. Each phase must pass automated gates before moving to next phase.
3. Manual testing happens only in the final phase after all code changes are complete.

## Global Rules
1. Do not start a new phase if current phase test gates are red.
2. Keep all changes backward-compatible until integration cutover phase.
3. Prefer additive changes behind feature flags where possible.
4. Preserve deterministic behavior for identical inputs.
5. If a gate is marked best-effort, keep it green but still run final manual spot-check.

## Environment Baseline
1. Activate Python env: `source .venv310/bin/activate`
2. Set project root on path: `export PYTHONPATH=.`
3. Run tests with `python3 -m pytest ...`

## Phase 0: Test Harness Hardening
- [ ] Add/confirm dedicated test modules for orchestration, materialization, and regression.
- [ ] Add fixture score set:
- `assets/test_data/amazing-grace-satb-verse1.xml`
- `assets/test_data/my-tribute.mxl/score.xml`
- [ ] Add helper assertions for:
- deterministic hashes
- measure-level lyric coverage
- appended part presence in MusicXML
- warning/status contract assertions (`ready_with_warnings`)

Automated gates:
- [ ] `python3 -m pytest tests/test_voice_parts.py -q`

## Phase 1: FullScoreAnalyzer Extensions
- [ ] Implement/extend analyzer payload used by planner:
- per-measure lyric coverage
- section-level source-candidate ranking hints
- verse-aware metadata (`verse_number`)
- [ ] Ensure parse responses can include analyzer output without breaking current clients.

Automated gates:
- [ ] `python3 -m pytest tests/test_voice_parts.py -k "analyze or coverage_map or source_candidates or verse" -q`
- [ ] `python3 -m pytest tests/test_api.py -k "voice_part_signals" -q`

## Phase 2: API Skeleton + Compatibility Wrapper
- [ ] Introduce `preprocess_voice_parts` orchestration entrypoint.
- [ ] Keep `prepare_score_for_voice_part` as compatibility wrapper.
- [ ] Normalize deprecated `voice_id` -> `voice_part_id`.
- [ ] Ensure existing synth path remains functional.

Automated gates:
- [ ] `python3 -m pytest tests/test_voice_parts.py -q`
- [ ] `python3 -m pytest tests/test_api.py -q`

## Phase 3: Plan Schema + Section Override Parsing
- [ ] Implement plan parser (`targets`, `actions`, overrides, verse flags, `copy_all_verses`).
- [ ] Validate enums and unknown action rejection.
- [ ] Validate section range boundaries and overlap precedence rules.
- [ ] Add explicit error/action codes for invalid plan payloads.
- [ ] Confirm status taxonomy alignment with spec (`ready`, `ready_with_warnings`, `action_required`, `error`).

Automated gates:
- [ ] `python3 -m pytest tests/test_voice_parts.py -k "plan or override or invalid or status_taxonomy" -q`
- [ ] `python3 -m pytest tests/test_api.py -k "action_required or invalid" -q`

## Phase 4: Split Engine Upgrade
- [ ] Implement `split_shared_note_policy` on `split_voice_part`.
- [ ] Default policy: `duplicate_to_all`.
- [ ] Optional policy: `assign_primary_only`.
- [ ] Preserve rest alignment and deterministic ordering.

Automated gates:
- [ ] `python3 -m pytest tests/test_voice_parts.py -k "split_shared_note_policy or split" -q`
- [ ] `python3 -m pytest tests/test_voice_parts.py -k "deterministic_split" -q`

## Phase 4.1: Propagation Strategy - `overlap_best_match`
- [ ] Implement weighted scoring formula and tie-breakers.
- [ ] Add deterministic tie resolution.

Automated gates:
- [ ] `python3 -m pytest tests/test_voice_parts.py -k "overlap_best_match or tie_break" -q`

## Phase 4.2: Propagation Strategy - `syllable_flow`
- [ ] Implement phrase boundary detection:
- breath/phrase marks
- rests above threshold
- section boundaries
- long holds
- [ ] Gate behavior behind `VOICE_PART_SYLLABLE_FLOW_ENABLED`.

Automated gates:
- [ ] `python3 -m pytest tests/test_voice_parts.py -k "syllable_flow or phrase_boundary" -q`

## Phase 4.3: Verse-Aware Propagation
- [ ] Implement `verse_number` default behavior.
- [ ] Implement `copy_all_verses=true` mode.
- [ ] Ensure single-verse default remains backward-compatible.

Automated gates:
- [ ] `python3 -m pytest tests/test_voice_parts.py -k "verse_number or copy_all_verses" -q`

## Phase 5: Validation Engine + Partial Success
- [ ] Implement validation metrics:
- `lyric_coverage_ratio`
- `source_alignment_ratio`
- `missing_lyric_sung_note_count`
- `lyric_exempt_note_count`
- [ ] Add `ready_with_warnings` status.
- [ ] Enforce thresholds:
- strict pass per spec
- partial success at `lyric_coverage_ratio >= 0.90`
- [ ] Include unresolved bars/notes summary payload.

Automated gates:
- [ ] `python3 -m pytest tests/test_voice_parts.py -k "validate or warnings or coverage or exempt" -q`
- [ ] `python3 -m pytest tests/test_api.py -k "ready_with_warnings" -q`

## Phase 5.5: Repair Loop Coordinator
- [ ] Implement bounded retry repair loop (max 2 retries).
- [ ] Pass summarized issue context to AI planner (no large raw payload dumps).
- [ ] Escalate to `action_required` after retry exhaustion.
- [ ] Gate behind `VOICE_PART_REPAIR_LOOP_ENABLED`.

Automated gates:
- [ ] `python3 -m pytest tests/test_voice_parts.py -k "repair_loop or retry or escalation" -q`
- [ ] `python3 -m pytest tests/test_api.py -k "repair_loop" -q`

## Phase 6: MusicXML Materializer (Append New Part)
- [ ] Implement append-only materializer for transformed output.
- [ ] Add `score-part` + `part` append logic.
- [ ] Use stable derived part id: `P_DERIVED_<short_transform_hash>`.
- [ ] Return `appended_part_ref` + `modified_musicxml_path`.
- [ ] Add golden-file/determinism assertions for append output.

Automated gates:
- [ ] `python3 -m pytest tests/test_voice_parts.py -k "materialize or append or modified_musicxml" -q`
- [ ] `python3 -m pytest tests/test_voice_parts.py -k "golden_musicxml or deterministic_appended_id" -q`

## Phase 7: Persistence, Fingerprinting, Idempotency, Locking
- [ ] Include `score_fingerprint` in transform cache key.
- [ ] Persist transformed artifacts and modified MusicXML path.
- [ ] Reuse identical transform output by hash.
- [ ] Add per-session lock for concurrent append safety.
- [ ] Mark automated concurrency checks as best-effort.

Automated gates:
- [ ] `python3 -m pytest tests/test_voice_parts.py -k "fingerprint or idempotent or reuse" -q`
- [ ] `python3 -m pytest tests/test_voice_parts.py -k "concurrency or lock" -q` (best-effort)

## Phase 8: Synthesize/Orchestrator Integration
- [ ] Route synth preprocess through new orchestration API.
- [ ] Ensure synth can target appended derived part directly.
- [ ] Remove caller-score in-place mutation behavior.
- [ ] Maintain old behavior when feature flag is disabled.

Automated gates:
- [ ] `python3 -m pytest tests/test_voice_parts.py -q`
- [ ] `python3 -m pytest tests/test_api.py -q`

## Phase 9: End-to-End Local Regression (Still No Manual)
- [ ] Run full targeted suite for SIG-6 plus affected synth flow.
- [ ] Generate derived outputs for `my-tribute` tenor path.
- [ ] Add E2E idempotency check:
- run same request twice
- assert second run reuses cached transform/materialized artifact.
- [ ] Run broader regression that includes non-SIG-6 synthesis behavior.

Automated gates:
- [ ] `python3 -m pytest tests/test_voice_parts.py tests/test_api.py -q`
- [ ] `python3 -m pytest tests/test_voice_parts.py -k "e2e_idempotent_reuse" -q`
- [ ] `python3 tests/create_voice_part_2_test_case.py`
- [ ] `python3 tests/create_soprano_test_case.py`
- [ ] `python3 -m pytest tests/test_slur_phonemizer.py -q`

## Phase 10: Final Human UAT (First Manual Checkpoint)
- [ ] Upload `my-tribute` score in local app.
- [ ] Request Tenor full lyric+notes generation.
- [ ] Confirm modified preview includes appended derived part.
- [ ] Confirm synth can sing appended part directly.
- [ ] Confirm modified MusicXML download works and reopens correctly.
- [ ] Concurrency spot-check: trigger two near-simultaneous same-target requests and verify no duplicate/conflicting append.

Exit criteria:
- [ ] All automated phases green.
- [ ] Final UAT approved.

## Suggested Feature Flags for Safe Rollout
1. `VOICE_PART_SECTION_OVERRIDES_ENABLED=true`
2. `VOICE_PART_REPAIR_LOOP_ENABLED=true`
3. `VOICE_PART_SYLLABLE_FLOW_ENABLED=true`
4. `VOICE_PART_PARTIAL_SUCCESS_ENABLED=true`
5. `VOICE_PART_DEPRECATE_VOICE_ID=false` (flip to `true` after migration window)
