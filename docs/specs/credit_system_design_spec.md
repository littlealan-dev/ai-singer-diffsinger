Credit System Design Spec
=========================

Scope
-----
Define the technical design for free-trial credits, including reservation, settlement, expiry locking, overdraft handling, and waitlist locking behavior. This spec complements `free_trial_implementation_plan.md` with concrete data model and API behavior.

Core Rules
----------
- 1 credit = 30 seconds of audio, charged as `ceil(actual_seconds / 30)`.
- Trial grant: 10 credits on first successful sign-in; expires in 14 days.
- Reservation gate: user must have `available_balance >= estimated_credits` to start synthesis.
- If `available_balance < estimated_credits`, synthesis is rejected (no capped audio).
- Expiry lock: once a reservation is created, expiry does not invalidate that reservation.
- Settlement: compute actual credits from server-measured audio duration.
- Overdraft: if balance becomes negative after settlement, the account is locked.
- Overdraft lockout: no upload/chat/modify/synthesize; allow play/download existing audio.
- Overdraft is permanent until subscription credits bring balance back to >= 0.

Data Model (Firestore)
----------------------
Collection: `users/{uid}`
```
{
  "credits": {
    "balance": 10,              // integer credits remaining (can go negative)
    "reserved": 0,              // integer reserved credits
    "expiresAt": "2026-01-22T00:00:00Z",
    "overdrafted": false,       // true when balance < 0 after settlement
    "trialGrantedAt": "2026-01-15T00:00:00Z"
  }
}
```

Collection: `credit_reservations/{jobId}`
```
{
  "userId": "abc123",
  "estimatedCredits": 4,
  "createdAt": "2026-01-15T00:00:00Z",
  "expiresAt": "2026-01-15T01:00:00Z",   // reservation TTL
  "status": "pending"                   // pending | settled | released
}
```

Collection: `credit_ledger/{entryId}` (optional audit)
```
{
  "userId": "abc123",
  "type": "grant|reserve|release|settle|overdraft|subscription",
  "jobId": "job123",
  "amount": -4,
  "balanceAfter": 6,
  "createdAt": "2026-01-15T00:00:00Z"
}
```

Credit Calculations
-------------------
- Estimated credits:
  - Derived server-side from parsed score metadata duration.
  - `estimated_credits = ceil(estimated_seconds / 30)`.
- Actual credits:
  - Derived server-side from waveform length: `len(waveform) / sample_rate`.
  - `actual_credits = ceil(actual_seconds / 30)`.

Reservation Flow (Atomic)
-------------------------
1) Begin transaction on `users/{uid}`.
2) Reject if `credits.overdrafted == true`.
3) Compute `available = balance - reserved`.
4) Reject if `available < estimated_credits`.
5) Increase `reserved += estimated_credits`.
6) Create `credit_reservations/{jobId}` with status `pending`.
7) Commit transaction.

Settlement Flow (Atomic)
------------------------
1) Begin transaction on `users/{uid}` and `credit_reservations/{jobId}`.
2) Verify reservation status == `pending`.
3) Decrease `reserved -= estimated_credits`.
4) Decrease `balance -= actual_credits`.
5) If `balance < 0`, set `overdrafted = true`.
6) Mark reservation status `settled`.
7) Commit transaction.

Release Flow (Atomic)
---------------------
For failures/cancellations/timeouts:
1) Begin transaction on `users/{uid}` and `credit_reservations/{jobId}`.
2) Verify reservation status == `pending`.
3) Decrease `reserved -= estimated_credits`.
4) Mark reservation status `released`.
5) Commit transaction.

Reservation TTL Reaper
----------------------
- Reservation TTL equals the session duration.
- Scheduled job runs every N minutes:
  - Find `credit_reservations` where `status == pending` and `expiresAt < now`.
  - Release reservation with the flow above.
- Prevents stranded reservations if a worker crashes.

Lockout Behavior
----------------
If `overdrafted == true`:
- Block: upload, chat, modify score, synthesize, reserve credits.
- Allow: play/download existing audio.
- UI should show "Balance negative. Subscribe to unlock."

Subscription Integration (Future)
---------------------------------
- On subscription purchase, add purchased credits to `balance`.
- If `balance >= 0`, set `overdrafted = false`.
- Trial credits carry forward automatically because they are already in `balance`.

Credit Method Exposure (High Level)
-----------------------------------
- Public MCP tool:
  - `estimate_credits(score) -> {estimated_seconds, estimated_credits, current_balance, balance_after, sufficient}`
- Internal-only methods (no REST):
  - `reserve_credits(user_id, job_id, estimated_credits) -> reservation`
  - `settle_credits(user_id, job_id, actual_seconds) -> {actual_credits, balance, overdrafted}`
  - `release_credits(user_id, job_id) -> reservation`

Security Rules (Notes)
----------------------
- Disallow client writes to `users/{uid}.credits` except via backend.
- Only allow server-side (Admin SDK) updates for reservations and settlement.
- Clients can read their own credit state.

Resolved
--------
- Synthesis timeout: 900 seconds (matches request timeout); releases reservation on timeout.
