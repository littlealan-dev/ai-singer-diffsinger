# Re-Quote on a Refused Synthesis LLD

**Status:** Agreed design, not yet implemented
**Scope:** When `synthesize` is called with a quote that no longer covers the
request, the backend prices the request itself and returns the new quote inside
the existing refusal, with what changed, in the same step. The LLM no longer has
to call `prepare_synthesis_quote` after a refusal.

## 1. Problem

A local turn: the user said "yes" to a quote; `synthesize` was refused
(`render_choices_changed`); the LLM called `prepare_synthesis_quote`; the next
LLM call wrote the reply from the new quote alone and said "Synthesis in
progress". Nothing was rendered or charged, because the quote follow-up allows
no tools, but the reply was false.

```
"yes"
  LLM call 1 → synthesize(Q1)             → refused       (call 2 sees why)
  LLM call 2 → prepare_synthesis_quote    → quote Q2      (call 3 sees only Q2)
  LLM call 3 → reply                      ← does not know the "yes" was for Q1
```

The backend already compares the stored quote with the request when it
refuses, so it knows both sides.

## 2. Decisions

| Topic | Decision |
|---|---|
| Refusal structure | Unchanged: `{status: "action_required", action: "synthesis_quote_refresh_required", message, reason}`, with two new **optional** fields |
| New optional field `quote` | Whatever `prepare_synthesis_quote` returns for the request, as-is: its success payload (`status: "quote_ready"`) or its failure payload (`status: "action_required"`, e.g. `unsupported_synthesis_language`) |
| New optional field `changed_choices` | `{field: {quoted, requested}}` between the refused quote and the new one; present only when a new quote was made and the choices differ |
| Who prices the request after a refusal | The backend, in the `synthesize` step, using the same code as `prepare_synthesis_quote` |
| What it prices | The `synthesize` call's own arguments, without `quote_id` and `confirmed_voicebank_override` |
| Follow-up | Always message-only. The LLM replies to `quote` exactly as it would to the quote tool's result, success or failure. A failed re-quote is final for this turn: calling a tool again could not fix it (an unsupported language stays unsupported) and could loop. |
| Other `synthesize` failures | Unchanged |
| LLM calls after a refused "yes" | One (the reply), instead of two |

## 3. Backend

### 3.1 One quote builder

The `prepare_synthesis_quote` branch of `_execute_tool_calls`
(`orchestrator.py`, from `if call.name == TOOL_PREPARE_SYNTHESIS_QUOTE`) moves
unchanged into a method:

```
async def _quote_synthesis(arguments, *, current_score, score_summary, session_id, user_id,
                           user_email, forced_voicebank_id, forced_language, expand_repeats,
                           score_player_takes, selected_explicit_verse_number)
        -> ToolExecutionResult
    target check → verse selection → forced voicebank → voicebank check → language
    → voice colour → lyric selection (complete, belongs to part, reparse) → solfege flags
    → drop resolved program assignments → program assignment check
    → price → create_synthesis_quote → active-quote metadata
    → quote_payload (status "quote_ready", …) → ToolExecutionResult(message-only, selection_resolved)
```

`prepare_synthesis_quote` calls it with `call.arguments`; nothing about that
tool's behaviour or payload changes.

### 3.2 The `synthesize` refusal

At today's refusal (`if not quote_valid:`):

```
action_required = { status: "action_required",                 # as today
                    action: "synthesis_quote_refresh_required",
                    message: backend_message("synthesis.quote_refresh_required"),
                    reason:  _synthesis_quote_mismatch_reason(…) }
requote = await _quote_synthesis(
              call.arguments minus {quote_id, confirmed_voicebank_override}, …)
quote = json.loads(requote.followup_prompt)                    # the quote tool's output, as-is
action_required["quote"] = quote
if quote.status == "quote_ready":
    changed = diff(old_quote.renderChoices, quote.render_choices)
    if changed: action_required["changed_choices"] = changed
return ToolExecutionResult(
    followup_prompt = json.dumps(action_required),
    action_required_payload = action_required,
    followup_message_only = True,                              # success or failure
    selection_resolved = (quote.status == "quote_ready"),
    score / session_state_changed = from requote)
```

`confirmed_voicebank_override` is dropped because it only asserts a voice the
old quote covered; the new quote uses the UI selection like any quote. On
success the quote builder records the new quote as the session's active quote,
so the next "yes" confirms it.

```
diff(old, new) = { key: {"quoted": old.get(key), "requested": new.get(key)}
                   for key in old ∪ new if old.get(key) != new.get(key) }
```

`reason` keeps today's codes (`render_choices_changed`, `ui_voicebank_changed`,
`score_version_changed`, `score_changed`, `quote_expired`,
`quote_already_reserved`, `quote_already_consumed`, `quote_not_found`,
`quote_owner_mismatch`, `quote_invalid`).

The quote tool's failure outputs that can appear in `quote`:

| `quote.action` (or reason) | Today, after the quote tool |
|---|---|
| target error (`requested_part_not_found`, …) | LLM may retry with another part |
| `verse_selection_required` | LLM asks the user for a verse |
| voicebank error | LLM may pick another voice |
| `unsupported_synthesis_language` | LLM explains; offers a supported option |
| `lyric_selection_required` | LLM may retry with a listed selection |
| `instrument_program_resolution_required` | LLM may retry with assignments |

Inside a refusal all of them are message-only: the LLM tells the user what is
needed, and the user's next message starts a fresh attempt. A retry the LLM
could have made on its own (another lyric selection, assignments) moves to the
next turn, in exchange for never looping on one that cannot succeed.

### 3.3 Example tool results

Re-quote succeeded:

```json
{
  "status": "action_required",
  "action": "synthesis_quote_refresh_required",
  "message": "The render choices or score changed since that price was quoted. Please review a fresh quote before generating.",
  "reason": "ui_voicebank_changed",
  "changed_choices": {
    "voicebank": { "quoted": "Diffsinger LIEE…", "requested": "Qixuan…" }
  },
  "quote": {
    "status": "quote_ready",
    "quote_id": "Q2",
    "part_id": "Men",
    "vocal_duration_seconds": 13.5,
    "vocal_part_credits": 1,
    "instrumental_credits": 0,
    "total_estimated_credits": 1,
    "estimate": { … },
    "render_choices": { "voicebank": "Qixuan…", "part_id": "Men", … },
    "available_credits": 6,
    "balance_after": 5,
    "instruction": "Present this exact itemized quote and ask for explicit confirmation. …"
  }
}
```

Re-quote failed:

```json
{
  "status": "action_required",
  "action": "synthesis_quote_refresh_required",
  "message": "The render choices or score changed since that price was quoted. Please review a fresh quote before generating.",
  "reason": "ui_voicebank_changed",
  "quote": {
    "status": "action_required",
    "action": "unsupported_synthesis_language",
    …exactly as the quote tool returns it…
  }
}
```

### 3.4 Unchanged

| Case | Behaviour |
|---|---|
| Blockers before the quote check (overdraft, voicebank, language, lyric selection, program assignments, preflight) | unchanged |
| Insufficient credits at reservation | unchanged |
| Quote check passes | reservation and rendering unchanged |
| Quote changes between the check and the reservation (`reserve_credits` returns `infra_error`) | unchanged |
| E2E credit bypass (no quote check) | unchanged |

## 4. Prompt changes (`system_prompt.txt`)

| Line (today) | Change |
|---|---|
| "If synthesis returns `action=synthesis_quote_refresh_required`, do not retry `synthesize`. Call `prepare_synthesis_quote` again, present the new quote, and wait for a fresh confirmation." | Replace with: "If synthesis returns `action=synthesis_quote_refresh_required`, synthesis did not start; never say it is in progress. Say in one sentence why the quoted price no longer applied, from `reason` and `changed_choices`. Then reply to its `quote` exactly as you would to a `prepare_synthesis_quote` result, success or failure: present a `quote_ready` and wait for a fresh confirmation, or explain an `action_required` and its next step. Do not call any tool in that response." |
| "`confirmed_voicebank_override` only ever asserts a voice … Setting the flag in that case does not authorize the render; the backend rejects it with `reason=ui_voicebank_changed`." | Unchanged: the refusal still has that `reason`; it now also carries the re-quote result. |

Kept as they are: the quote presentation rules and the rules for each quote
failure (they apply to the nested `quote`), "If that context says `none` and the
user is confirming … call `prepare_synthesis_quote` once", "Never state or imply
that synthesis has started … unless this response actually calls `synthesize`",
and the active-quote instruction in the dynamic context (`credits.py`).

## 5. Tests

| Test | Change |
|---|---|
| `test_stale_quote_blocker_lets_the_model_prepare_a_fresh_quote` | Rewritten: a stale "yes" gets the refusal with a nested `quote_ready`; one message-only reply, no tool call; nothing reserved |
| `test_confirmed_override_cannot_render_a_voice_the_quote_never_covered` | Still expects `action_required` with `reason == "ui_voicebank_changed"`; also expects a nested `quote` for the UI's voice and `changed_choices.voicebank` |
| `test_llm_prompt.py` (asserts `action=synthesis_quote_refresh_required`) | Still passes; adds assertions for the new rule |
| New | render choices changed → `changed_choices` lists exactly the differing key |
| New | score edited after the quote → `reason: score_version_changed`, a nested `quote`, no `changed_choices` |
| New | "yes" again after the take was rendered → `reason: quote_already_consumed` with a nested `quote`; no second take |
| New | the request cannot be quoted (voice does not support the language) → the refusal with the quote tool's `action_required` nested as-is, message-only, no quote created, no further tool call |
| New | the nested `quote` equals what `prepare_synthesis_quote` returns for the same arguments, apart from `quote_id`, for a success and for a failure |

## 6. Build order

1. Move the quote branch into `_quote_synthesis`; `prepare_synthesis_quote` calls it. Existing tests pass unchanged.
2. Use it in the `synthesize` refusal; add the optional `quote` and `changed_choices`; make the follow-up message-only.
3. Prompt rules.
4. Backend suite against the baseline; e2e core spec.
