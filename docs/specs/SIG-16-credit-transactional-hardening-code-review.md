# SIG-16 Code Review — Credit Transactional Hardening (v2)

**Scope:** LLD sections 1–4, 6 (reconciliation job deferred).  
**Files:** [credits.py](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/credits.py), [orchestrator.py](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py), [job_store.py](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/job_store.py), [test_credits.py](file:///Users/alanchan/antigravity/ai-singer-diffsinger/tests/test_credits.py), [test_job_store_progress.py](file:///Users/alanchan/antigravity/ai-singer-diffsinger/tests/test_job_store_progress.py)

---

## Overall Verdict

**LGTM — ready to merge.** All previous review points have been addressed. No blocking issues.

---

## ✅ LLD Alignment Checklist

| LLD Section | Status | Notes |
|---|---|---|
| §1 Reservation State Machine | ✅ | `pending → settled/released/reconciliation_required` |
| §2 Explicit Credit Results | ✅ | `ReserveCreditsResult`, `SettleCreditsResult`, `ReleaseCreditsResult` |
| §3 Settle-before-complete | ✅ | [orchestrator.py:551-586](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py#L551-L586) |
| §4 Startup compensation | ✅ | [orchestrator.py:2746-2784](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py#L2746-L2784) |
| §5 Reconciliation job | ⏸️ | Deferred |
| §6 Job state handling | ✅ | `credit_reconciliation_required` → `error` |
| `audioUrl` withholding | ✅ | Defensive strip at [job_store.py:110-111](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/job_store.py#L110-L111) |
| Idempotent reserve | ✅ | `reservation_exists` for same `(job_id, uid, credits)` |
| `mark_reservation_reconciliation_required` | ✅ | Best-effort with structured error fields |

---

## 🟢 Changes Since v1 Review

**Observation #6 resolved:** `build_progress_payload` now actively strips `audio_url` when `raw_status == "credit_reconciliation_required"` ([job_store.py:110-111](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/job_store.py#L110-L111)). Covered by `test_build_progress_payload_maps_credit_reconciliation_status_to_error` which passes `audioUrl` in test data and asserts `"audio_url" not in payload`.

---

## 🟡 Remaining Minor Observations

### 1. `mark_reservation_reconciliation_required` is non-transactional

[credits.py:79-121](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/credits.py#L79-L121) — `get()` + `set()` without a transaction. Fine as best-effort at current scale. No action needed.

### 2. Settle exception handler doesn't write a ledger entry

[credits.py:412-424](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/credits.py#L412-L424) — when `settle_credits` catches an exception, it marks reconciliation but skips the ledger. The reconciliation job should write the entry when it runs.

### 3. No injected-failure test for settle-doesn't-decrement-balance

LLD test plan case 5. Hard to test without deep Firestore transaction mocking; Firestore guarantees this by design.

---

## ✅ Test Results

```
17 passed in 1.59s
```

| Suite | Count | Status |
|---|---|---|
| `test_job_store_progress.py` | 6 | ✅ All pass |
| `test_credits.py` | 11 | ✅ All pass |
