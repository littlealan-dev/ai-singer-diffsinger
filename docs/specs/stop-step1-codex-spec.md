# Stop, step 1 without splits: Codex task specs

Design: `docs/specs/sing-all-process-backend-hld.md` §7.1, on branch `docs/sing-all-process-hld` (f7c3700). This is task A of the work that can start before planner v2: Stop for chat replies, takes and export mixes, plus refusing an upload while work runs. Stopping a split is a later task (B), after planner v2 merges, because the v2 branch rewrites the split's job code.

There are two Codex tasks, run one after the other:
- **A1 Backend**: statuses, stop point, `POST /sessions/{id}/stop`, the worker and GPU checks, the chat-turn check, the upload refusal.
- **A2 UI**: the Stop button, how a stop shows, and upload disabled while work runs. A2 starts after A1 is merged into the parent branch.

Parent branch for both: `feature/sing-all`, cut from `origin/main`. Codex PRs go into it, never into main.

Differences from the HLD, both chosen to keep this safe:
- **Splits aren't stoppable in step 1.** Stop is hidden while a split runs, and the endpoint reports a running split as `not_stoppable`. An upload during a split is still refused.
- **The worker never cancels its own task while the GPU call is in flight.** Cancelling an awaited MCP call can make the router treat it as a failure and restart the GPU process. Instead, the GPU process stops itself at its next stage check, and the worker checks at its own points.

---

## A1. [Codex] Stop, backend: stop chat replies, takes and export mixes, and refuse uploads while work runs

### Parent

Branch: `feature/sing-all`, cut from `origin/main`. PR into it, never main.
Design: `docs/specs/sing-all-process-backend-hld.md` §7.1 on branch `docs/sing-all-process-hld`. Where this spec and the HLD differ, this spec wins.

### Rule

Each job has one stop point. Before it, Stop ends the job and refunds it. After it, Stop is too late, and the job completes as if Stop was never pressed. One transaction decides which, so the answer is definite.

```python
def stop_job(job_id):                       # JobStore, one transaction
    if job.status not in {"queued", "running"} or job.pastStopPoint: return "too_late"
    job.status = "cancelled"; job.cancelledBy = "user"; job.step = "cancelled"
    job.message = backend_message("job.stopped"); job.progress = 1.0; job.stoppedAt = now
    return "stopped"

def pass_stop_point(job_id):                # JobStore, one transaction
    if job.status == "cancelled": return False      # stop here, publish nothing
    job.pastStopPoint = True
    return True                                     # from here the job always completes
```

| Work | Stop point | Before it, Stop does |
|---|---|---|
| Chat reply (and add-solfège, a tool inside the turn) | The reply is saved | Ends before the next LLM call or tool call. The user's message stays, with a "Stopped" reply. A call in progress finishes and its output is dropped |
| Single take (`jobKind` synthesis, local or premium route) | Just before the vocoder | The take is dropped, no audio is published, the reservation is released in full |
| Export mix | Just before the final upload of the mix file | The mix is dropped, the reservation is released in full |
| Split (`jobKind` preprocess) | Not stoppable in this task | Reported as `not_stoppable`, nothing changes |

### Change

#### 1. Job store, `src/backend/job_store.py`

- Add `stop_job(job_id) -> "stopped" | "too_late"` and `pass_stop_point(job_id) -> bool`, each one Firestore transaction, as above.
- Add `list_active_jobs(*, user_id, session_id) -> list[(job_id, data)]`: jobs of that user and session with status `queued` or `running`.
- `release_job_for_retry` also deletes `pastStopPoint`, because the job goes back to the queue and starts again.
- `build_progress_payload`: a `cancelled` job with `cancelledBy == "user"` reports `status: "stopped"`. Every other `cancelled` job still reports `error`, as today.

#### 2. Stop endpoint, `src/backend/main.py`

`POST /sessions/{session_id}/stop`, owner only, same auth as the chat endpoint.

```python
def stop_session_work(session_id, user_id):
    sessions.set_metadata(session_id, "stopTurnId", id of the latest user entry in history)   # if any
    results = []
    for job_id, job in job_store.list_active_jobs(user_id, session_id):
        if job.jobKind == "preprocess":
            results.append(not_stoppable); continue
        r = job_store.stop_job(job_id)
        if r == "stopped":
            release the reservation (existing release path for that job kind; idempotent)
            if the release fails: existing mark_reservation_reconciliation_required path
            append an assistant history entry: what stopped and the credits refunded
        results.append(r)
    return {"result": overall(results), "jobs": [{jobId, jobKind, result, refundedCredits}]}

overall:  "stopped" if any stopped; else "too_late" if any too_late;
          else "not_stoppable" if any not_stoppable; else "nothing_running"
```

`stopTurnId` is always written. It only affects a turn with that id that is still running, so it's harmless when no turn is running.

#### 3. Chat turn, `src/backend/orchestrator.py`

- Add `ChatTurnStopped(RuntimeError)`.
- Check `stopTurnId == this turn's id` (one session read):
  - in `_call_llm_client_generate`, beside the existing turn-limit check, before the call;
  - in `_execute_tool_calls`, before each tool call.
- The check applies only to the chat turn's own calls. It must **not** apply to LLM calls made by a preprocess job, even one started from this turn, because splits aren't stoppable yet. Add the turn id to `_LlmTurnBudget` and mark the budget set in `handle_chat` as stoppable; the dispatched preprocess job's budget is not. If a split job copies the chat turn's context, it must not inherit the stoppable mark.
- When `ChatTurnStopped` is raised, `handle_chat` returns `{"type": "chat_text", "message": backend_message("chat.stopped"), "stopped": true}` and appends that reply to history, so the LLM knows next turn. No tool runs and no job starts after the check fails. Existing error handlers must not turn it into a `chat_error`.
- A read error in the check is logged and the turn continues.

#### 4. Takes

Worker, `run_dispatched_synthesis_job` and `_run_synthesis_job`:
- Right before the synthesize MCP call, re-read the job. If it's `cancelled`, end the run: no render, no retry.
- The lease heartbeat already stops renewing when the job is no longer `running`. When it finds the job cancelled by the user, it records that for the run, so that no retry happens. It must **not** cancel the run's task while the MCP call is in flight.
- A stopped run is never handed back for retry. The existing `release_for_retry` must return False for it, including when shutdown cancels the task afterwards.
- The run ends with the job still `cancelled` (status, step and message from `stop_job` are kept), and its reservation released. The existing `_release_and_finalize_synthesis_job` may be reused; `already_released` counts as done.

GPU process:
- `src/mcp/handlers.py` `handle_synthesize`: when it has a Firestore job id (`progress_job_id`), build two callbacks with `JobStore`: `should_stop()` reads the job and returns True when it's `cancelled`; `pass_stop_point()` calls the store method.
- `src/api/synthesize.py` `synthesize()`: a new optional `stop_check: Callable[[str], bool]` argument. Call it before align, durations, pitch, variance and the acoustic model (mel). When it returns True, return `{"status": "stopped", "stage": <name>}`.
- `src/api/inference.py` `synthesize_audio()`: a new optional `before_vocoder: Callable[[], bool]` argument, called between `synthesize_mel` and `vocode`. False means return `{"status": "stopped", "stage": "vocoder"}` without running the vocoder. `synthesize()` passes `pass_stop_point` here.
- With no job id, or on any read error, the checks are skipped or treated as "carry on", and logged. A failed `pass_stop_point` write also carries on: the completion safety net (5) still catches a stop.
- `{"status": "stopped"}` is a normal tool result. The MCP router must not count it as a failed call or restart the process. The orchestrator's `_synthesize` turns it into a new `SynthesisStopped` exception, handled like the cancelled path above, with no retry.
- The same checks apply wherever `handle_synthesize` runs, including the premium worker route. If a route has no job store, it renders as today.

A queued take that's stopped is never claimed, because `claim_dispatched_job` only claims `queued` or `running` jobs. Keep that, and test it.

#### 5. Completion safety net, `src/backend/credits.py`

`settle_credits_and_complete_job` and `settle_export_mix_credits_and_complete_job` read the job inside their transaction **before** the reservation checks. If its status is `cancelled`, they return a new status `cancelled`, write nothing, and publish nothing. The callers treat `cancelled` as final: they don't release again or overwrite the job as `failed`. This catches an old worker during a deploy, or a missed check.

#### 6. Export mix, `src/backend/main.py` `_run_export_mix_job`

Just before `upload_file` writes the mix: `if not pass_stop_point(job_id)`, delete the local temp file, make sure the reservation is released (idempotent), and end with the job still `cancelled`.

#### 7. Upload refusal, `POST /sessions/{session_id}/upload`

After auth and the session check, before parsing: if `list_active_jobs` returns any job, including a split, refuse with `409 {"code": "work_running", "message": backend_message("upload.work_running")}`. Demo songs go through the same endpoint, so they are covered.

#### 8. Messages, `src/backend/messages/en.properties`

Add `job.stopped`, `chat.stopped` and `upload.work_running` (for example "Stopped.", "Stopped.", "Something is still running in this song. Stop it first, or wait for it to finish."), plus the history entry for a stopped job, with the refunded credits.

### Scope

May change: `src/backend/job_store.py`, `src/backend/main.py`, `src/backend/orchestrator.py`, `src/backend/credits.py`, `src/mcp/handlers.py`, `src/api/synthesize.py`, `src/api/inference.py`, `src/backend/mcp_client.py` (only to treat `stopped` as a normal result, if needed), `src/backend/messages/en.properties`, tests.

Must not change: the preprocess job flow and its completion, any prompt, pricing, credit amounts, `src/mcp/tools.py` tool schemas, env files, deploy scripts, the UI.

Tests use synthetic scores and made-up words. The repo is public: never put job ids, session ids, score titles, part names from user scores or lyrics into code, tests, commits or the PR.

### Done when

Each new test fails on the parent commit.

- **Store.** `stop_job` on a queued or running job returns `stopped` and sets `cancelled`, `cancelledBy`, `stoppedAt`. On a finished job, or one with `pastStopPoint`, it returns `too_late` and changes nothing. `pass_stop_point` returns False on a cancelled job and True otherwise, setting `pastStopPoint`. `release_job_for_retry` clears it.
- **Race.** With `pass_stop_point` and `stop_job` called in either order on one job, exactly one wins: either the job completes and Stop says `too_late`, or it's cancelled and nothing is published.
- **Progress.** A user-stopped job reports `stopped`; a shutdown-cancelled job still reports `error`.
- **Endpoint.** Nothing running gives `nothing_running`. A running take gives `stopped`, the reservation is released and `refundedCredits` equals it, and history has the entry. A take past its stop point gives `too_late` and later completes and is charged. A running split gives `not_stoppable` and the split is unchanged. Another user's session gives 404.
- **Chat turn.** With `stopTurnId` set during a turn (a fake LLM client sets it on its first call), the next LLM call is not made, no tool runs, and the reply is `stopped: true` and saved to history. A tool call is skipped the same way. A preprocess job started from a turn whose id is in `stopTurnId` still makes its LLM calls. A `stopTurnId` from an earlier turn doesn't affect a new turn.
- **Take, worker.** A take stopped while queued is never claimed. A take stopped before the MCP call never calls synthesize. A take stopped during the call ends `cancelled`, with no retry and no process restart, and its reservation released, also when shutdown follows.
- **Take, GPU process.** `synthesize()` with a `stop_check` that turns True before each of the five stages returns `stopped` at that stage and runs no later stage. With `before_vocoder` returning False, the vocoder isn't called. With no job id, synthesis runs as today. A job store read error carries on.
- **Safety net.** Both settle-and-complete functions return `cancelled` for a cancelled job and write nothing; the caller leaves the job `cancelled`.
- **Export mix.** A mix stopped before the upload writes no file to storage, ends `cancelled`, and is refunded. One past its stop point completes and is charged.
- **Upload.** An upload, or a demo-song pick, with a queued or running job of any kind in the session gives 409 `work_running`, and the session's score is unchanged. With no job running, it works as today.

### Checks (paste baseline vs after outputs in the PR)

```
P=/Users/alanchan/antigravity/ai-singer-diffsinger/.venv310/bin/python
FIRESTORE_EMULATOR_AUTOSTART=0 $P -m pytest tests/test_backend_api.py tests/test_job_store_progress.py tests/test_job_dispatch.py tests/test_credits.py tests/test_credit_retry.py tests/test_mcp_progress_firestore.py tests/test_mcp_server.py tests/test_mcp_end_to_end.py tests/test_premium_synthesis_worker.py tests/test_synthesis_pricing.py tests/test_audio_mix.py -q -p no:cacheprovider
```

Add your new test files to the same command. Measure the baseline on the parent commit first. Its known failures are not caused by this work; do not fix them, and the same ones must remain with no new ones. `git diff --check` is clean.

### Out of scope

- Stopping a split (task B, after planner v2 merges).
- Stopping inside a stage. A stage in progress runs to its end.
- The UI (task A2).
- Sing All.

---

## A2. [Codex] Stop, UI: a Stop button in the chat, and no upload while work runs

### Parent

Branch: `feature/sing-all`, after A1 is merged into it. PR into it, never main.
Design: `docs/specs/sing-all-process-backend-hld.md` §7.1, and A1 above for the API.

### Change, `ui/src/MainApp.tsx` and `ui/src/api.ts` (or wherever the session API calls live)

- **Stop button** beside Send, shown while `chatTurnInProgress` is true, except while the running job is a split (`job_kind === "preprocess"` in progress). Square icon, `aria-label="Stop"`, `data-testid="stop-work"`. One tap, no confirmation. While the request is in flight it shows as "Stopping…" and is disabled.
- **Click:** `POST /sessions/{id}/stop`.
  - A chat turn in flight: its own chat response comes back with `stopped: true`. Show it as a normal reply and clear `chatTurnInProgress`.
  - A take or export mix: the progress poll returns `status: "stopped"`. The bubble shows the job's message ("Stopped.") and the refund, not an error, and `chatTurnInProgress` clears.
  - `too_late`: a short notice, "Too late to stop: it's just finishing". The normal result then arrives as usual.
  - `nothing_running` or `not_stoppable`: no change, and the button hides once the state updates.
- **Upload while work runs:** the upload and demo-song controls are disabled while `chatTurnInProgress` is true, with the tooltip "Stop the running work first". A 409 `work_running` from the backend shows its message in the chat, the same way other upload errors show.
- A stopped take is not added to the player's tracks.

### Scope

May change: `ui/src/MainApp.tsx`, the UI API client file, CSS for the button, e2e specs.

Must not change: the backend, other components' behaviour, the mixer, the browser mix bounce.

### Done when

New Playwright specs in `ui/e2e/specs/`, following the patterns of `progress-poll-recovery.spec.ts` and `synthesis-stream-recovery.spec.ts`:
- Stop during a chat reply: the Stop button shows, a click sends the stop request, the reply shows as stopped, and Send works again.
- Stop during a take: the progress poll returns `stopped`, the bubble shows the stopped message, no track is added, and Send works again.
- Too late: the notice shows, then the take's audio arrives as normal.
- No Stop button while a split runs.
- Upload and demo-song controls are disabled while work runs. A 409 `work_running` shows its message.

### Checks (paste outputs in the PR)

```
cd ui && npm run build
cd ui && npx playwright test e2e/specs/<your new spec files> e2e/specs/progress-poll-recovery.spec.ts e2e/specs/synthesis-stream-recovery.spec.ts e2e/specs/core-singing-regression.spec.ts
```

Measure on the parent commit first; known failures must stay the same with no new ones.

### Out of scope

- The "New song or replace?" popup (step 2, Songs).
- Stop during a split, and Sing All's confirmation.
