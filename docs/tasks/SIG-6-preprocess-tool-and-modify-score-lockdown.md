# SIG-6 Task List: Expose `preprocess_voice_parts` + Finalize `modify_score` Lockdown

## Goal
- Expose `preprocess_voice_parts` as a first-class MCP/public tool (and top-level API export if needed).
- Fully decouple preprocessing from synthesis so `synthesize` is render-only.
- Enforce stateless "preprocessing required" validation in `synthesize`.
- Complete visibility/security cleanup so `modify_score` remains internal-only and unreachable by LLM/public MCP paths.

## Scope
- In scope:
  - MCP tool schema + handler + routing for `preprocess_voice_parts`.
  - Orchestrator/tool contract updates so LLM can use typed preprocessing flow.
  - `synthesize` refactor to remove internal preprocessing and add stateless readiness validation.
  - Remaining hardening for `modify_score` exposure.
  - Docs/tests updates for new tool surface and removed public `modify_score`.
- Out of scope:
  - New planning algorithms.
  - Model-level acoustic/timing changes.

## Workstream A: Expose `preprocess_voice_parts` as MCP Tool

### A1. API Surface
- [ ] Confirm/standardize function signature in `src/api/voice_parts.py`:
  - input: `score`, `request` (plan/direct payload), optional flags/context
  - output: typed preprocessing result (`status`, transformed score refs, warnings/errors, metrics)
- [ ] Export at top-level API (if required by existing pattern):
  - update `src/api/__init__.py`

### A2. MCP Tool Definition
- [ ] Add `preprocess_voice_parts` to `src/mcp/tools.py` with:
  - input schema (typed `score` + preprocessing request payload)
  - output schema (status contract + transformed score metadata + diagnostics)
- [ ] Add clear field descriptions for:
  - lint/preflight findings
  - plan execution summary
  - post-flight validation summary
  - status (`ready`, `ready_with_warnings`, `action_required`, `error`)

### A3. MCP Handler + Server Wiring
- [ ] Add handler in `src/mcp/handlers.py` for `preprocess_voice_parts`.
- [ ] Register handler in handler map.
- [ ] Add tool to CPU mode allowlist in `src/mcp_server.py`.
- [ ] Add router mapping in `src/backend/mcp_client.py` (`preprocess_voice_parts -> cpu`).

### A4. Orchestrator Integration
- [ ] Update orchestrator tool allowlist and tool use flow:
  - enable LLM to call `preprocess_voice_parts` explicitly
  - enforce explicit preprocess -> synth sequence for complex parts
- [ ] Define expected sequencing:
  - `parse_score` -> `preprocess_voice_parts` -> `synthesize`
- [ ] Ensure action-required responses from preprocess are surfaced to user without silent fallback.

### A5. Prompt + Contract Docs
- [ ] Update `src/backend/config/system_prompt.txt` tool instructions to include `preprocess_voice_parts`.
- [ ] Update design/spec docs that describe MCP surface and workflow:
  - `docs/specs/backend_architecture.md`
  - `docs/specs/SIG-6-ai-orchestrated-voice-part-workflow.md`
  - `docs/specs/SIG-6-ai-orchestrated-voice-part-low-level-design.md`
  - `docs/tasks/SIG-6-preprocess-workflow-refinement.md` references/backlinks

## Workstream B: Decouple Preprocess from Synthesize + Stateless Enforcement

### B1. Remove Hidden Preprocessing from Synthesize
- [ ] Remove internal `preprocess_voice_parts(...)` invocation from `src/api/synthesize.py`.
- [ ] Ensure `synthesize(...)` only performs render/inference for the provided score payload and selected part.
- [ ] Keep backward-compatible error messaging so callers get actionable next steps.

### B2. Implement Stateless Readiness Validation in Synthesize
- [ ] Add pre-synthesis validation using score metadata only (no session state dependency):
  - complexity signal from `voice_part_signals` (multi-voice / missing lexical lyric indicators)
  - derived part detection by:
    - index delta vs original part count from `score_summary`
    - derived transform metadata key presence in `voice_part_transforms`
- [ ] Validation rule:
  - if preprocessing-required part is requested and target is not derived, return:
    - `status: "action_required"`
    - typed code, e.g. `preprocessing_required_for_complex_score`
    - message instructing caller to run `preprocess_voice_parts`.
- [ ] Ensure rule is stateless and reproducible for direct API + MCP callers.

### B3. Workflow Contract and Error Semantics
- [ ] Normalize status contract between preprocess/synthesize:
  - preprocess may return `ready`, `ready_with_warnings`, `action_required`, `error`
  - synth returns `action_required` when score/part is not "ready to sing"
- [ ] Ensure orchestrator surfaces this as workflow guidance, not opaque failure.

## Workstream C: Complete `modify_score` Internal-Only Lockdown

### C1. Public Surface Audit
- [ ] Verify `modify_score` is absent from:
  - MCP `tools/list` in all public modes
  - MCP tool allowlists and router mappings
  - orchestrator LLM tool allowlist
  - system prompt tool instructions
- [ ] Decide and document whether `modify_score` remains callable only by direct internal Python API.

### C2. Defensive Guardrails
- [ ] Add regression guard test: public MCP call to `modify_score` must fail with explicit error.
- [ ] Add regression guard test: LLM tool response containing `modify_score` is ignored/rejected.
- [ ] Ensure no fallback path auto-executes arbitrary score code from chat payload.

### C3. Docs Cleanup
- [ ] Remove/replace stale references to public `modify_score` in docs and examples.
- [ ] Add short note describing why it is internal-only (safety/typed ops contract).

## Testing Plan

### Unit / Contract
- [ ] `tests/test_mcp_server.py`
  - tool listing includes `preprocess_voice_parts`
  - `modify_score` not listed/not callable publicly
- [ ] new/updated tests for `src/mcp/tools.py` schema contract of `preprocess_voice_parts`
- [ ] `tests/test_synthesize*.py` / `tests/test_api.py`
  - synth rejects complex non-derived targets with `action_required`
  - synth accepts equivalent derived targets statelessly
- [ ] `tests/test_backend_integration.py`
  - no dependency on LLM `modify_score`
  - parse -> preprocess -> synth explicit flow
  - verify no implicit preprocess in synth path

### E2E
- [ ] E2E run on representative multi-voice score:
  - verify preprocess status and diagnostics returned
  - verify downstream synthesis uses preprocessed score artifact
- [ ] Negative path:
  - intentionally infeasible plan yields `action_required` from preprocess stage

## Acceptance Criteria
- [ ] `preprocess_voice_parts` is callable as a dedicated MCP tool with typed input/output.
- [ ] `synthesize` no longer performs hidden preprocessing.
- [ ] `synthesize` enforces stateless readiness and returns typed `action_required` for raw complex targets.
- [ ] UI/LLM workflow runs explicit `parse_score` -> `preprocess_voice_parts` -> `synthesize` sequence.
- [ ] `modify_score` is fully hidden from LLM/public MCP and covered by regression tests.
- [ ] Docs/specs reflect current tool surface and behavior.

## Risks / Notes
- Schema size for preprocess output may be large; keep required fields stable and allow `additionalProperties` only where needed.
- Decoupling synth from preprocess is a behavior change; legacy callers may need migration guidance.
- Do not reintroduce free-form score mutation path in public tooling.
