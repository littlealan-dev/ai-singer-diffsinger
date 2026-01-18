# Credit System Manual Test Cases

## Scope
Validate free-trial credits behavior, estimate/reserve/settle flow, overdraft lock, and UI credit display. Waiting list is out of scope.

## Preconditions
- Firebase Auth and Firestore emulators running (for local testing) or production configured.
- Backend and frontend running.
- Clean test user (new account) available.
- A valid MusicXML score for synthesis.

## Test Data
- MusicXML file with ~45 seconds of audio duration.
- MusicXML file with ~90 seconds of audio duration.
- MusicXML file with ~300+ seconds of audio duration.

## Test Cases

### 1) Trial Credits Granted On First Sign-In
Steps:
1. Sign in with a new user account (Google or Email link).
2. Open the app `/app`.
3. Open Firestore or Credits header.

Expected:
- User document created with `credits.balance = 10`, `credits.reserved = 0`.
- `credits.expiresAt` is about 7 days in the future.
- Credits header shows available credits > 0.


### 2) Credits Estimate Required Before Synthesis
Steps:
1. Upload a MusicXML score.
2. Ask the assistant to synthesize without first requesting estimate.

Expected:
- Assistant responds with a message requesting an estimate before proceeding.
- No synthesis job starts.


### 3) Estimate Returns Correct Cost And Balance
Steps:
1. Upload a ~45s score.
2. Ask: "Estimate credits for this score."

Expected:
- Estimated seconds ~45.
- Estimated credits = 2 (ceil(45/30)).
- Current balance and projected balance shown.


### 4) Reserve Credits On Synthesis
Steps:
1. Use a score with ~90 seconds.
2. Ask for estimate, then confirm to synthesize.
3. Check Firestore `users/{uid}.credits.reserved` immediately after starting.

Expected:
- Reserved credits increase by estimated amount.
- A `credit_reservations/{jobId}` document exists with status `pending`.


### 5) Settle Credits After Audio Completes
Steps:
1. Use a score with ~90 seconds.
2. Estimate and confirm synthesis.
3. Wait for audio completion.
4. Check Firestore credits and ledger.

Expected:
- Reserved credits decrease back to 0.
- Balance decreases by actual credits (ceil(actual_seconds/30)).
- `credit_ledger` has a `settle` entry for this job.


### 6) Insufficient Credits Prevent Synthesis
Steps:
1. Use a score with estimated credits > available balance.
2. Ask for estimate, confirm to synthesize.

Expected:
- Synthesis rejected with insufficient credits message.
- No reservation created.


### 7) Overdraft Lock On Long Render
Steps:
1. Use a score with actual duration longer than available credits to push balance below zero.
2. Estimate and confirm synthesis.
3. Wait for completion.
4. Try a new synthesis or upload.

Expected:
- Balance becomes negative; `credits.overdrafted = true`.
- Further syntheses are blocked with lockout message.


### 8) Credits Expiry Blocks Synthesis
Steps:
1. Manually set `credits.expiresAt` in the past (or wait until expiry).
2. Attempt to estimate and synthesize.

Expected:
- Synthesis is blocked with expiry message.
- Credits header shows expired state.


### 9) Reservation Release On Failure
Steps:
1. Force synthesis failure (e.g., invalid score or simulate backend failure).
2. Start synthesis with a valid estimate.
3. Verify reservation release.

Expected:
- Reservation marked `released`.
- `credits.reserved` restored.
- Ledger has a `release` entry.


### 10) Credits Header States
Steps:
1. Set balance to >2, then <=2, then 0, then negative.
2. Observe header each time.

Expected:
- Normal state for >2.
- Warning state for <=2.
- Danger state for 0, expired, or overdrafted.


## Notes
- If testing on production, use a disposable account and reset credits in Firestore after tests.
- For emulator runs, ensure `FIRESTORE_EMULATOR_HOST` is set for backend processes.
