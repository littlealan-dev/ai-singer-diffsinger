# SIG-16 Credit Transactional Hardening LLD — Review

**Reviewer:** AI  
**LLD:** [SIG-16-credit-transactional-hardening-lld.md](file:///Users/alanchan/antigravity/ai-singer-diffsinger/docs/specs/SIG-16-credit-transactional-hardening-lld.md)  
**Linear Issue:** [SIG-16](https://linear.app/sightsinger/issue/SIG-16/harden-credit-reservationsettlement-workflow-for-transactional) (status: Backlog)

---

## Overall Assessment

**Verdict: Strong design, ready for implementation with minor adjustments.**

The LLD accurately identifies all four real gaps in the codebase and proposes a reasonable compensation-based approach. The gap analysis is validated against source; the proposed result types, workflow reordering, and reconciliation model are sound. Below are specific observations.

---

## ✅ Gap Analysis — Confirmed Against Source

All four gaps are **real and accurately described**:

| Gap | LLD Claim | Source Validation |
|-----|-----------|-------------------|
| **Gap 1** — reserve before job startup | Credits reserved before `_start_synthesis_job(...)` | ✅ Confirmed: [orchestrator.py:794-826](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py#L794-L826) — `reserve_credits` runs at L794, then `_start_synthesis_job` at L824. No compensation if `_start_synthesis_job` fails. |
| **Gap 2** — completed before settle | Job marked `completed` before `settle_credits(...)` | ✅ Confirmed: [orchestrator.py:365-378](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py#L365-L378) — `update_job(..., status="completed")` at L365, `settle_credits` at L378. |
| **Gap 3** — ambiguous credit API outcomes | `reserve → bool`, `settle → (int, bool)`, `release → bool` | ✅ Confirmed: [credits.py:95-307](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/credits.py#L95-L307) — exceptions are caught and collapsed to `False` / `(0, False)`. |
| **Gap 4** — no reconciler | No sweeper for expired `pending` reservations | ✅ Confirmed: no reconciliation code exists anywhere in the backend. |

---

## 🟡 Review Comments

### 1. Gap 1 is narrower than described

> [!NOTE]
> The LLD says reserve happens in `orchestrator.py` before `_start_synthesis_job(...)`, which is correct. However, the actual call site is inside `_execute_tool_calls()` (L794), not at the top of `_start_synthesis_job()` itself.

This is not a factual error—the LLD says "in orchestrator.py"—but the fix description in **§4 (Startup Compensation)** should ideally reference `_execute_tool_calls` as the wrapping site, not `_start_synthesis_job`, since the reserve call lives **outside** that function.

### 2. `_start_synthesis_job` can throw *before* the asyncio task starts

`_start_synthesis_job` performs several `await` calls (snapshot fetch, `create_job`, `update_job`) before `asyncio.create_task(...)`. If any of those raise, the reservation is stranded. The LLD's §4 correctly identifies this, but should note that the wrapping try/release needs to be in `_execute_tool_calls`, around lines 794–826.

### 3. Gap 2: completion-before-settle — ordering is correct in LLD, but consider the UX edge case

The proposed reordering (§3) says: *"only if settlement succeeds, mark job completed."*

This is correct for consistency, but means a user whose synthesis succeeds but settlement fails will get **no audio at all** (the job stays non-completed). The LLD does mention the `credit_reconciliation_required` state and a user message, but:

> [!IMPORTANT]
> **Open question 1** (output file access for reconciliation-required jobs) should be resolved before implementation. The recommended answer is: **retain the audio file in storage but withhold the `audioUrl` from the progress payload** until reconciliation completes. This lets ops replay settlement without re-synthesizing.

### 4. Proposed result types — `overdrafted` and `expired` on `ReserveCreditsResult`

The `ReserveCreditsResult` includes `"overdrafted"` and `"expired"` as separate statuses. This is good, because the current code checks both conditions but the caller (orchestrator) currently pre-checks `overdrafted` and `is_expired` separately. After this change, the pre-check can be removed and the reserve call becomes the single source of truth for rejection reasons.

### 5. `reservation_exists` status — Open Question 3

> [!TIP]
> **Recommend: treat as idempotent success (return `"reserved"`)** if the existing reservation matches the same `job_id` and amount. The current code generates the `job_id` with `uuid4().hex` before calling `reserve_credits`, so a duplicate-reserve for the same `job_id` would only happen during a retry, and idempotent success is the safest design.

### 6. Reconciliation trigger (Open Question 2)

> [!TIP]
> **Recommend: Firebase Scheduled Function (cron-triggered endpoint)**. An in-process background task coupled to the API server risks being lost on server restart. A separate ops worker is overkill at current scale. A scheduled function scanning every 5–15 minutes is the simplest reliable option.

### 7. `settle_credits` currently returns `(0, False)` for missing reservations

In [credits.py:193-194](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/credits.py#L193-L194), if the reservation doesn't exist, settle returns `(0, False)` — the same value as an infra error. This is exactly the ambiguity the LLD's `SettleCreditsResult` fixes. The proposed statuses (`reservation_missing`, `already_settled`, `already_released`) cover all the cases correctly.

### 8. Missing edge case in Failure Handling Matrix

The matrix covers 5 scenarios but omits one:

> **Missing: Reserve succeeded → synthesis in progress → worker/server crashes**

This is partially covered by §5 (reconciler), but the matrix should explicitly call out that this is the reconciler's primary recovery scenario. Without this, the matrix implies the only recovery for a crash is through the `expiresAt` TTL, which is correct but should be stated.

### 9. Test plan — consider adding a negative-balance settlement test

Test case 4 ("synth succeeds, settle fails, job does not become completed") should also verify that the balance is **not decremented** when settlement fails. The current `settle_credits` catches exceptions and returns `(0, False)`, but the proposed design should verify the transactional rollback.

### 10. Rollout plan — step 1 could be a breaking interface change

Step 1 says "Introduce structured result types behind existing call sites." Since `reserve_credits` changes from `bool → ReserveCreditsResult` and `settle_credits` from `Tuple[int, bool] → SettleCreditsResult`, all callers must be updated atomically.

> [!WARNING]
> The rollout plan describes this as "behind existing call sites," but this is a **signature-breaking change**. Both `orchestrator.py` (the only caller) and any tests must be updated in the same commit. Recommend making this explicit.

---

## Alignment: LLD vs. Linear Issue

| Linear Acceptance Criterion | LLD Coverage |
|-|-|
| No reservation stranded without reconciliation | ✅ §4 + §5 |
| Job not reported completed unless settlement succeeds | ✅ §3 |
| Settlement/release failures surfaced with correlation fields | ✅ §2 + §6 (Logging) |
| Expired/stuck pending reconciled by deterministic recovery | ✅ §5 |
| Tests cover partial-failure and recovery scenarios | ✅ §Test Plan (8 cases) |

All five acceptance criteria are addressed. The proposed scope in the Linear issue (4 items) maps cleanly to LLD sections 2–5.

---

## Summary of Recommendations

1. **§4**: Reference `_execute_tool_calls` as the compensation wrapping site
2. **Open Q1**: Retain audio, withhold `audioUrl` until reconciliation
3. **Open Q3**: Treat `reservation_exists` as idempotent success
4. **Open Q2**: Use Firebase Scheduled Function for reconciliation
5. **Failure Matrix**: Add explicit crash-recovery scenario
6. **Test Plan**: Add negative-balance-not-decremented assertion for failed settle
7. **Rollout Plan**: Make the signature-breaking nature of step 1 explicit
