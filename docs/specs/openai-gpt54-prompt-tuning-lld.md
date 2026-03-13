# OpenAI GPT-5.4 Prompt Tuning LLD

## 1. Goal

Improve preprocess-plan generation quality and consistency when the backend LLM provider is OpenAI, specifically `gpt-5.4`.

This LLD is prompt-only.

Scope:

- prompt structure changes for:
  - [`src/backend/config/system_prompt.txt`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/system_prompt.txt)
  - [`src/backend/config/system_prompt_lessons.txt`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/system_prompt_lessons.txt)
  - planner-facing rendering of lint and postflight rules from [`src/api/voice_part_lint_rules.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/voice_part_lint_rules.py)
- quality improvements for:
  - tool-routing correctness
  - preprocess plan completeness
  - repair-turn locality
  - final-plan structural safety

Out of scope:

- provider transport changes
- MCP orchestration changes
- native OpenAI function/tool calling
- code changes in this iteration

## 2. Motivation

The current prompt stack is strong on coverage, but it is optimized for rule completeness, not for GPT-5.4-specific instruction following efficiency.

Current strengths:

- strong JSON-only contract
- detailed workflow constraints
- rich preprocess lessons
- canonical lint and postflight rule descriptions

Current weaknesses for GPT-5.4:

- high rule density is spread across long prose blocks
- “done” criteria for preprocess planning are implicit rather than centralized
- self-check logic exists, but is not framed as an explicit mandatory verification loop
- tool-routing prerequisites are present, but not grouped as a short dependency ladder
- lint/postflight rule payload is canonical but not prioritized for planning usefulness

## 3. External Guidance

This design is based on current official OpenAI guidance as of 2026-03-13.

Primary reference:

- OpenAI prompt guidance: https://developers.openai.com/api/docs/guides/prompt-guidance

Relevant guidance from that doc:

- use modular prompt structure with clearly named sections
- add explicit completion contracts
- add explicit verification loops before final output
- add clear tool-persistence / workflow continuation rules
- improve prompt clarity before increasing reasoning effort
- keep output contracts unambiguous and close to the top of the instruction hierarchy

Inference for this system:

- GPT-5.4 will likely perform better if the current prompt is restructured into named blocks with stronger “must verify before answer” framing
- preprocess planning quality is more likely to improve from prompt structure than from higher reasoning effort alone

## 4. Current Prompt Architecture

Prompt assembly today:

- [`src/backend/llm_prompt.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_prompt.py)
  - loads:
    - `system_prompt.txt`
    - `system_prompt_lessons.txt`
    - rendered lint rules
    - rendered postflight validation rules
  - combines them into static prompt text
  - appends runtime context separately

This is already good for OpenAI prompt caching because:

- instructions are static
- tool schema is static
- lint rules are static
- runtime score context is separate

Therefore this LLD focuses on improving the contents and structure of the static prompt prefix, not the prompt split itself.

## 5. Design Summary

For GPT-5.4, retain the same facts and policies, but reorganize them into a more explicit instruction hierarchy.

Main prompt changes:

1. Add explicit XML-style or tag-like instruction blocks.
2. Add a preprocess completion contract.
3. Add a mandatory verification loop before `preprocess_voice_parts`.
4. Add a compact workflow dependency ladder for tool routing.
5. Add a short planner-critical lint summary before the full canonical lint list.
6. Keep the full canonical rule dump, but treat it as reference material after the planner summary.

This is a restructuring design, not a policy rewrite.

## 6. Proposed Prompt Blocks

### 6.1 `system_prompt.txt`

Recommended top-level block order:

1. `<role>`
2. `<capabilities>`
3. `<response_contract>`
4. `<workflow_dependencies>`
5. `<tool_persistence_rules>`
6. `<preprocess_policy>`
7. `<verse_policy>`
8. `<review_and_synthesize_policy>`
9. `<action_required_policy>`
10. `<tool_output_interpretation>`

Reason:

- GPT-5.4 benefits from front-loaded contract and workflow structure
- the most important constraints become easier to identify and follow

### 6.2 `system_prompt_lessons.txt`

Recommended top-level block order:

1. `<planner_scope>`
2. `<hard_rules>`
3. `<preprocess_completion_contract>`
4. `<verification_loop>`
5. `<repair_locality_policy>`
6. `<common_failure_modes>`
7. `<plan_self_check>`

Reason:

- the lessons file should act as the planner’s operational playbook
- completion and verification should appear before failure examples

## 7. Prompt Improvements

## 7.1 Add an explicit preprocess completion contract

Today, completion criteria are distributed across many rules.

For GPT-5.4, add a dedicated compact block such as:

```text
<preprocess_completion_contract>
- A preprocess plan is complete only if:
  - requested visible targets are covered across intended sung ranges
  - sections are gap-free and non-overlapping
  - every non-rest section has an explicit source path
  - lyric policy matches whether native target lyrics exist
  - no new P0 structural issue is introduced
- If any condition fails, revise before emitting the plan.
</preprocess_completion_contract>
```

Expected effect:

- fewer “first plausible answer” plans
- better alignment with runtime lint/postflight expectations

## 7.2 Add a mandatory verification loop

Current prompt contains self-check ideas, but they should be promoted into an explicit required step.

Recommended block:

```text
<verification_loop>
- Before returning a preprocess_voice_parts tool call:
  1. Re-expand sections to a per-measure action table.
  2. Verify no gaps and no overlaps.
  3. Verify every non-rest target measure has a source path.
  4. Verify strategy and lyric policy match parser facts.
  5. Verify the plan would not obviously violate known lint/postflight rules.
- If verification fails, revise the plan before answering.
</verification_loop>
```

Expected effect:

- better structural completeness
- better local consistency in section boundaries
- fewer lint-triggering omissions

## 7.3 Add a compact workflow dependency ladder

Current tool-routing rules are comprehensive but spread out.

For GPT-5.4, add a short explicit dependency ladder in `system_prompt.txt`:

```text
<workflow_dependencies>
- Missing score -> ask for upload.
- Multi-verse score without explicit verse -> ask user to choose verse.
- Requested verse differs from current verse -> call reparse.
- Complex target -> call preprocess_voice_parts.
- After successful preprocess on complex score -> ask user to review/confirm.
- Only after confirmation -> call synthesize.
</workflow_dependencies>
```

Expected effect:

- fewer premature synthesize calls
- fewer skipped reparse or verse-selection prerequisites

## 7.4 Add tool persistence rules

GPT-5.4 guidance recommends explicit continuation rules for multi-step tasks.

Recommended block:

```text
<tool_persistence_rules>
- Do not skip a prerequisite just because the final action seems obvious.
- Continue the workflow until the current stage is genuinely complete.
- Do not stop at preprocess initiation when a verification or review step is still required.
- Do not collapse multiple dependent workflow stages into one response unless explicitly allowed.
</tool_persistence_rules>
```

Expected effect:

- stronger multi-turn workflow discipline
- fewer invalid “preprocess + synthesize in one turn” attempts

## 7.5 Add a planner-critical lint summary

Current lint/postflight prompt rendering is canonical and detailed:

- `Rule code`
- `Name`
- `Definition`
- `Fails when`
- `Suggested fix`

That full format should stay, but GPT-5.4 will likely plan better if the prompt first includes a shorter planner-facing summary of the highest-impact rules.

Recommended addition before the canonical dump:

```text
<planner_critical_rules>
- Preserve structural correctness over lyric quality.
- No gaps, no overlaps, no source-less non-rest sections.
- Visible exported targets must visibly claim sung measures.
- Local repairs should stay local to the failing range unless completeness requires more.
- Do not introduce a new P0 structural issue while fixing lyric coverage.
</planner_critical_rules>
```

Expected effect:

- better prioritization
- less token spent re-deriving rule importance from the long canonical list

## 7.6 Tighten repair-locality wording

The lessons prompt already contains strong repair-locality language.

For GPT-5.4, make it even more operational:

- explicitly say “default to local repair”
- explicitly say “full-plan rewrites require explicit diagnostics evidence”

Recommended addition:

```text
<repair_locality_policy>
- Default to local repair inside reported repair scopes.
- Preserve all unrelated targets and ranges unless a completeness rule requires change.
- Full-plan rewrites are allowed only when diagnostics show the plan structure itself is globally wrong.
</repair_locality_policy>
```

Expected effect:

- fewer over-broad rewrites
- better stability across bounded repair attempts

## 8. Impact on System Prompt

Recommended changes to [`system_prompt.txt`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/system_prompt.txt):

- keep all existing rules and policies
- reorganize into named blocks
- move JSON response contract earlier and isolate it
- add:
  - workflow dependency ladder
  - tool persistence rules
  - explicit “only stop when current stage is complete” wording

No policy changes required to:

- verse selection logic
- preprocess-vs-synthesize policy
- action-required handling
- tool-output interpretation

Those parts are already functionally correct.

## 9. Impact on Lessons Prompt

Recommended changes to [`system_prompt_lessons.txt`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/system_prompt_lessons.txt):

- preserve all hard rules
- insert preprocess completion contract near the top
- promote verification loop into a first-class required step
- move common failure modes below the positive operational contract

Why:

- GPT-5.4 tends to perform better when success criteria are stated before cautionary examples

## 10. Impact on Lint Rule Rendering

Recommended prompt-only improvement:

- prepend a compact planner summary before the existing canonical lint and postflight rule dump

Do not remove the canonical dump.

Reason:

- planner summary helps the model prioritize
- canonical list remains available for detailed repair behavior

Minimal rendering strategy:

- new short static header section
- existing `render_lint_rules_for_prompt()` and `render_postflight_validation_rules_for_prompt()` remain unchanged initially

Lower-risk variant:

- add the planner summary in `_load_system_prompt()` before the canonical render output
- do not rewrite `voice_part_lint_rules.py` renderers yet

## 11. Reasoning Effort Guidance

For GPT-5.4, prompt structure should improve before increasing reasoning effort.

Recommended starting settings:

- ordinary routing / action-required turns:
  - `none` or `low`
- initial preprocess planning:
  - `low`
- preprocess repair planning:
  - `low` or `medium`

Do not default to `high` or `xhigh` for preprocess planning in v1.

Reason:

- the task is mostly structured planning under explicit constraints
- bad prompt structure will not be fixed efficiently by higher reasoning effort

## 12. Expected Benefits

Expected quality improvements:

- fewer premature incomplete preprocess plans
- better section completeness
- better adherence to local repair scopes
- fewer invalid multi-stage tool sequences
- better structural safety during repair

Expected performance improvements:

- less prompt ambiguity
- lower need for elevated reasoning effort
- more stable cached prompt reuse due to cleaner static instruction prefix

## 13. Risks

1. Over-structuring may make the prompt more rigid than necessary.

Mitigation:

- preserve existing policy content
- change organization first, not semantics

2. Duplicating rules across summaries and canonical lists could create contradictions.

Mitigation:

- planner summary must be a strict abstraction of canonical rules, not an alternate rule set

3. Prompt growth could reduce effective room for dynamic context.

Mitigation:

- keep new summary blocks short
- prefer restructuring over adding large new prose sections

## 14. Recommended Implementation Sequence

Phase 1:

- add named prompt blocks
- add preprocess completion contract
- add verification loop
- add workflow dependency ladder

Phase 2:

- add planner-critical lint summary
- add repair-locality block

Phase 3:

- evaluate GPT-5.4 outputs on existing preprocess-plan scenarios
- only then consider reasoning-effort tuning

## 15. Review Checklist

When reviewing prompt edits, verify:

- JSON output contract remains unchanged
- tool-routing prerequisites remain explicit
- preprocess completion is clearly defined
- verification loop is mandatory, not optional
- structural correctness priority remains above lyric quality
- repair-locality language remains consistent with bounded repair policy
- canonical lint/postflight rules remain the source of truth

## 16. Final Recommendation

For GPT-5.4, improve preprocess-plan quality by restructuring the static prompt into clearer operational blocks rather than by changing workflow semantics.

Highest-priority additions:

- preprocess completion contract
- mandatory verification loop
- workflow dependency ladder
- tool persistence rules

These changes are low-risk because they preserve the current orchestrator contract and MCP behavior while making the prompt more compatible with OpenAI’s current prompt-guidance recommendations.

