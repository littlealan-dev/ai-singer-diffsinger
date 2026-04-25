# SightSinger Billing Spec — Q&A / Decision Log

This document captures the question-and-answer review used to finalize the Stripe billing spec for SightSinger.

---

## 1. Free-plan migration scope

### Question
The spec says legacy non-paying users keep their current balance until the next free refresh.
Should that apply only to users who have never paid, or to all users who are currently not on an active paid subscription?

Related:
- if a legacy user is currently overdrafted, should migration leave that as-is until next refresh?
- should the next free refresh clear overdraft once due?

### Answer
Apply the migration rule to **all users who are currently not on an active paid subscription**, not only users who have never paid.

Reason:
- simpler rule
- easier migration
- avoids weird edge cases for previously-paid-but-now-free users
- current entitlement state matters more than historical payment history

For legacy overdrafted users:
- preserve their current state during migration
- do not do a special-case balance repair during migration
- let the next due free refresh normalize the account

At the next free refresh:
- reset balance to the free-tier monthly allowance
- leave `reserved` untouched
- normalize the account through normal recurring refresh logic rather than a one-off migration repair

---

## 2. Source of `creditRefreshAnchor`

### Question
The spec says:
- new free users: first entry into permanent free plan
- legacy free users: original account creation time
- annual paid: monthly refresh from anchor

We should define exactly what anchor is for a user who:
- starts free
- later subscribes annual
- later cancels back to free

Do we want one stable anchor across the user’s lifetime, or separate anchors per entitlement mode?

### Initial answer
Use **one stable per-user anchor across the user’s lifetime**.

Meaning:
- new permanent free users: anchor = first entry into permanent free plan
- legacy users: anchor = account creation timestamp
- annual paid monthly refreshes use the same anchor
- returning to free after cancellation uses the same anchor

The clean mental model is:
- **anchor determines when refresh happens**
- **active entitlement determines what allowance is granted**

### Important clarification
For the case:

- starts free
- subscribes annual
- cancels the next day
- annual subscription remains active until period end

The system must:
- continue granting the **annual plan monthly allowance** on each anchor date until the subscription actually expires
- **not** apply free-tier refresh while the annual paid subscription is still active
- only switch back to free-tier allowance after paid entitlement actually ends

So the refined rule is:

- one stable per-user anchor across lifetime
- active paid entitlement takes precedence over free-tier refresh
- cancel-at-period-end does **not** switch monthly credit amount immediately
- only subscription expiry changes the entitlement level used on future refreshes

### Final answer
Use **one stable anchor across the user’s lifetime**, but determine the refresh amount from the user’s **effective entitlement at refresh time**.

Rule:
- anchor decides **when**
- entitlement decides **how much**

For annual subscriptions canceled at period end:
- continue granting annual-plan monthly credits until the subscription actually ends
- do not switch to free-tier refresh while the annual subscription remains active
- after paid entitlement ends, continue using the same anchor but apply the free-tier allowance

---

## 3. Monthly paid plans and `invoice.paid`

### Question
For monthly plans, the spec uses `invoice.paid` as the grant trigger.

Should we explicitly define idempotency as one grant per invoice?

Recommended guard:
- do not write a paid grant ledger row if one already exists for the same `stripeInvoiceId`

### Answer
Yes.

For **monthly paid plans**:
- exactly one grant should be written per Stripe invoice
- idempotency should be enforced using `stripeInvoiceId`

Recommended rule:
- do not write a paid grant ledger entry if one already exists for the same `stripeInvoiceId`

This should be explicit in the LLD.

Related distinction:
- monthly paid grants are invoice-driven
- annual monthly scheduler grants are not invoice-driven per month; they are scheduler-driven by anchor date

---

## 4. User document shape

### Question
Should `activePlanKey` be duplicated in both `billing` and `credits`, or only under `billing`?

Suggested approach:
- `billing.activePlanKey` is the source of truth
- `credits.monthlyAllowance` is cached operational state only

### Answer
Agree with the suggested approach.

Final decision:
- `billing.activePlanKey` is the source of truth
- `credits.monthlyAllowance` is cached operational state only
- do **not** duplicate `activePlanKey` under `credits`

Reason:
- cleaner data model
- avoids duplication drift
- billing answers “what plan is the user on?”
- credits answers “what is the operational balance state?”

---

## 5. Legacy fields cleanup

### Question
The spec deprecates:
- `trialGrantedAt`
- `trial_reset_v1`
- 30-day expiry semantics

Should launch:
- keep those old fields untouched for backward compatibility, or
- clear/stop writing them in migration?

### Initial answer
Recommended default:
- stop writing them in the new flow
- tolerate them if present
- do not make new flow depend on them

### Important refinement
There is current logic where, for a **trial-expired user**, the first login again grants the current trial credits and 30 days immediately.

Under the new permanent free-tier design:

- if a legacy user’s trial is **already expired** when permanent free tier launches
- on their **first login after launch**
- immediately convert them to permanent free tier
- immediately grant **8 credits**
- set the **free-tier anchor to that first re-login / conversion date**

This should happen **once only**.

If a legacy trial account is **not yet expired** when permanent free tier launches:
- preserve current credits unchanged
- do not immediately convert them at launch
- keep the original trial/account registration date as the anchor cadence
- transition naturally into free-tier recurring behavior at the next due anchor boundary

### Final answer
Legacy trial handling splits into two cases.

#### Case A — legacy trial not yet expired at launch
- preserve current state and current credits
- do not immediately convert to free tier on launch day
- keep the original trial/account registration date as the anchor cadence
- transition into normal permanent free-tier behavior at the next due anchor/refresh boundary

#### Case B — legacy trial already expired at launch
- on the first login after launch, immediately convert the account to permanent free tier
- immediately grant 8 credits
- set free-tier anchor = that first re-login / conversion date
- future monthly free refreshes follow that new anchor

Additional rule:
- this “expired legacy user upgraded to free tier on re-login” grant must be **one-time only**
- add a persistent conversion marker so it cannot retrigger on later logins

Legacy fields themselves:
- stop writing old trial-expiry semantics in the new flow
- tolerate old fields if present
- do not let new entitlement logic depend on them

---

## 6. `expiresAt` semantics

### Question
Current credits implementation uses `expiresAt`.

With the new permanent free tier and recurring plans, should:
- `credits.expiresAt` become unused / null for all recurring balances, or
- it still have meaning for future temporary grants?

### Answer
Treat `credits.expiresAt` as **legacy / unused for recurring plan balances**.

Meaning:
- recurring free-tier balances do not rely on `expiresAt`
- recurring paid balances do not rely on `expiresAt`
- annual monthly refresh balances do not rely on `expiresAt`

Keep the field only for:
- backward compatibility
- possible future one-off or temporary grants

So the recurring billing flow should not depend on `expiresAt`.

---

## 7. Scheduler due-query strategy

### Question
The spec says:

- query users whose `billing.nextCreditRefreshAt <= now`, or whose refresh is otherwise due from anchor/date computation

Should we choose one canonical strategy?

Suggested approach:
- always persist `billing.nextCreditRefreshAt`
- scheduler queries only that field
- anchor/date computation is used when initializing or repairing, not every run

### Answer
Agree.

Final decision:
- always persist `billing.nextCreditRefreshAt`
- scheduler queries only `billing.nextCreditRefreshAt <= now`
- anchor/date computation is used when:
  - initializing a user
  - migrating a user
  - repairing inconsistent state

Reason:
- simpler scheduler
- cheaper scanning
- more predictable operations
- avoids recomputing due state for every user on every run

---

## 8. Stripe event storage

### Question
`stripe_events/{eventId}` looks right.

Should we store full raw payload or only a compact summary?

Suggested approach:
- summary + processing metadata by default
- avoid storing full raw event unless needed for debugging/compliance

### Answer
Agree.

Final decision:
- store summary + processing metadata by default
- do not store full raw Stripe event payload unless there is a specific future need

Recommended stored fields:
- `eventId`
- `type`
- `created`
- `processed`
- `processedAt`
- `customerId`
- `subscriptionId`
- `invoiceId`
- `checkoutSessionId` when relevant
- `userId`
- small payload summary blob if useful

Reason:
- keeps Firestore lean
- enough for idempotency and debugging
- avoids unnecessary payload storage

---

## 9. No parallel subscriptions / stale Firestore vs Stripe

### Question
The spec rejects checkout if user already has active paid subscription.

If Stripe has customer state but Firestore is stale, which side wins for checkout blocking?

Suggested approach:
- Firestore first for fast path
- optionally verify with Stripe when state is ambiguous or stale

### Answer
Agree.

Final decision:
- use **Firestore first** for the fast path
- verify with Stripe only when local state is ambiguous or suspected stale

Examples of ambiguous/stale cases:
- billing doc missing but Stripe customer exists
- recent webhook failure suspected
- local status incomplete or obviously inconsistent
- support/repair flow

Normal case:
- trust Firestore

Edge case:
- verify with Stripe before allowing checkout

---

## 10. Shared helper modules

### Question
Because billing is implemented as separate HTTP functions, should the LLD explicitly define shared helper modules for:
- plan catalog
- Stripe client init
- billing-state persistence
- grant/idempotency helpers

### Answer
Yes.

The LLD should explicitly define shared helper modules to avoid duplication across functions.

Recommended shared modules:
- plan catalog
- Stripe client initialization
- billing-state persistence
- grant/idempotency helpers
- refresh-date calculation helpers
- webhook event processing helpers

Reason:
- reduces code duplication
- keeps function handlers thin
- makes future migration/refactor easier

---

## 11. Free-plan refresh cadence

### Question
What is the difference between:
- rolling every 30 days
- monthly from first refresh anchor

And which should be used?

### Answer
Use **monthly from refresh anchor**.

Reason:
- better matches Stripe’s calendar-anchored monthly billing model
- more intuitive to users
- cleaner for account display and support reasoning

Final rule:
- free-plan refresh is monthly from the user’s refresh anchor date
- if the anchor day does not exist in a later month, use the last day of that month
- this is intentionally calendar-month anchored, not a rolling 30-day interval

---

## 12. Stripe subscription monthly cadence

### Question
Does Stripe also use monthly-from-anchor instead of rolling 30 days?

### Answer
Yes.

Stripe monthly subscriptions are calendar-anchored using a billing cycle anchor, not “every 30 days” by default.

That is why matching free-tier refresh to a monthly anchor is the cleaner product rule.

---

## 13. Refresh while `reserved > 0`

### Question
The earlier review suggested the reset rule around reserved credits needed one more safety rule.

Could `reserved > newBalance` happen because:
- audio is generating in-flight
- credits are reserved
- refresh runs mid-generation
- balance resets lower than current reservation
- settlement later can produce a negative or inconsistent balance?

Or is there another scenario?

### Answer
Yes, that is exactly the main scenario.

Typical case:
1. reserve credits for a job
2. job is still running
3. monthly refresh becomes due
4. balance resets while reservation is still active
5. later the reservation settles

That can produce temporary inconsistency if refresh is applied mid-transaction.

Other possible scenarios:
- delayed worker / delayed callback
- partial outage between reserve and settle/release
- multiple concurrent reservations

This does **not** mean reservations stay forever.
It means refresh timing can overlap with in-flight reservations.

### Follow-up question
If `reserved > 0`, can we just not refresh balance then?
If so, how should it reset later?

### Answer
Yes.

For v1:
- do **not** refresh while `reserved > 0`
- do **not** introduce a `refreshPending` flag
- run the scheduler once daily
- if refresh is due and `reserved == 0`, apply refresh
- if refresh is due and `reserved > 0`, skip and retry on the next daily run

This is the chosen simplification.

### Follow-up question
Do we really need a `refreshPending` flag?

### Answer
No, not for v1.

A daily scheduler can determine:
- refresh due based on anchor + `nextCreditRefreshAt` / `lastCreditRefreshAt`
- only refresh when `reserved == 0`
- otherwise retry next day

So the final choice is:
- **no `refreshPending` flag**
- daily retry is enough for v1

---

## 14. Remaining open items before finalizing the spec

### Question
What else was still open after the earlier review?

### Answer
At that point, the main open items were:

- Cloud Functions endpoint shape
- monthly paid-plan refresh source
- no parallel subscriptions rule
- legacy user migration rule
- exact portal return URL
- exact rule for disabling early supporter plans

These were later closed.

---

## 15. Custom upgrade flow

### Question
Can we build our own plan upgrade flow and logic while still not allowing parallel or stacked subscriptions?

### Answer
Yes, that is viable in the future.

Recommended position for initial launch:
- do **not** implement custom upgrade flow yet
- block second checkout if user already has active paid subscription
- direct them to Manage Billing or support

Future path:
- custom upgrade flow can be built later in SightSinger app/backend
- still maintain exactly one active subscription
- still keep Stripe portal for payment method updates, invoices, cancellation

Final decision:
- **custom upgrade flow is deferred and out of scope for initial launch**

---

## 16. Early supporter plan disable rule

### Question
What rule should be used for turning off early supporter pricing?

### Answer
Use a simple environment flag:
- `CHOIR_EARLY_SUPPORTER_ENABLED=true/false`

The decision to turn it off will be made manually, based on analytics/business judgment.

No date-based or first-N-users rule is required for v1.

---

## 17. Portal return URL

### Question
What should the portal return URL be?

### Answer
No strong preference was expressed, so the standard choice is:

- `/app/settings/billing`

This becomes the default return target from the Stripe Customer Portal.

---

## 18. Final migration rule refinement for legacy trial users

### Question
There is current logic where, for trial-expired users, the first login grants current trial credits and 30 days immediately.

Under the new permanent free tier:
- if an expired user logs in again, should we “upgrade” the expired trial to free tier immediately and grant 8 credits immediately?
- should that first re-login date become the future monthly free credit anchor?
- while still-active trial accounts should keep their existing credit unchanged until the next anchor based on original trial/account registration date?

### Answer
Yes.

Final rule:
- if the legacy trial was already expired at launch:
  - on first re-login after launch, immediately convert to permanent free tier
  - immediately grant 8 credits
  - set free-tier anchor = that re-login / conversion date
  - this must happen one time only

- if the legacy trial was not yet expired at launch:
  - preserve current credits/state
  - do not immediately convert on launch day
  - keep original registration/trial cadence
  - transition naturally into normal free-tier behavior at the next due boundary

This is the final legacy migration refinement.

---

## 19. Final combined implementation assumptions

These are the final assumptions agreed for the spec and LLD.

### Billing placement
- Billing runs on Cloud Functions for Firebase (2nd gen)

### Endpoint shape
Separate HTTP functions:
- `createCheckoutSession`
- `createPortalSession`
- `stripeWebhook`

Scheduled:
- `refreshCredits`

### Monthly paid refresh
- monthly paid plan grants are triggered by `invoice.paid`
- exactly one grant per `stripeInvoiceId`

### Free / annual refresh
- free-tier refreshes and annual-plan monthly refreshes are driven by scheduler

### Refresh anchor
- one stable anchor across user lifetime
- active entitlement determines allowance amount
- paid annual entitlement continues monthly grants until actual expiry, even if canceled at period end

### No parallel subscriptions
- no stacking
- no second checkout while active paid subscription exists
- custom upgrade flow deferred

### Legacy trial migration
- still-active trial at launch: preserve current state until natural boundary
- expired trial at launch: first re-login converts to free tier immediately, grants 8 credits, and sets new free-tier anchor

### Legacy field policy
- stop writing legacy trial fields in new flow
- tolerate if present
- do not depend on them

### `expiresAt`
- legacy / unused for recurring balances

### Scheduler due strategy
- canonical due query uses `billing.nextCreditRefreshAt`

### Reserved credits
- if `reserved > 0`, skip refresh and retry next daily run
- no `refreshPending` flag for v1

### Stripe event storage
- summary + processing metadata only

### Firestore vs Stripe
- Firestore first
- Stripe only when local state is ambiguous or stale

---

## 20. Recommended answer block sent back to the coding agent

```md
Answers / decisions:

1. Free-plan migration scope
- Apply the migration rule to all users who are currently not on an active paid subscription, not only users who have never paid.
- Preserve existing balance until the next due free refresh.
- If a legacy user is overdrafted, leave it as-is until next refresh; the next free refresh normalizes the account to the standard free balance rule.

2. creditRefreshAnchor
- Use one stable per-user anchor across the user’s lifetime.
- New permanent free users: anchor = first entry into permanent free plan.
- Legacy users:
  - if still-active trial at launch: preserve current cadence from original registration/trial date
  - if expired trial at launch: on first re-login after launch, convert to free tier, grant 8 credits, anchor = that conversion date
- Annual paid monthly refresh uses the same stable anchor.
- Returning to free after cancellation uses the same anchor.
- Paid entitlement takes precedence over free-tier refresh: an active annual subscription, even if cancelAtPeriodEnd is true, continues to receive annual monthly grants until actual expiry.

3. Monthly paid plans and invoice.paid
- Yes: exactly one paid grant per Stripe invoice.
- Guard rule: do not write a paid grant ledger row if one already exists for the same stripeInvoiceId.

4. User document shape
- billing.activePlanKey is the source of truth.
- credits.monthlyAllowance is cached operational state only.
- Do not duplicate activePlanKey under credits.

5. Legacy fields cleanup
- Stop writing legacy trial fields in the new flow.
- Tolerate them if present.
- Do not make the new flow depend on them.

6. expiresAt semantics
- Treat credits.expiresAt as legacy / unused for recurring free-plan and paid-plan balances.
- Keep for backward compatibility only.

7. Scheduler due-query strategy
- Canonical strategy: always persist billing.nextCreditRefreshAt.
- Scheduler queries only that field.
- Anchor/date computation is used when initializing, migrating, or repairing, not every run.

8. Stripe event storage
- Store summary + processing metadata by default.
- Do not store full raw event payload unless needed later.

9. No parallel subscriptions / stale state
- Firestore wins for fast path.
- Verify with Stripe only when local state is ambiguous or suspected stale.

10. Upgrade flow
- Custom upgrade flow is deferred and out of scope for initial launch.

11. Shared helper modules
- Yes, the LLD should define shared helper modules for plan catalog, Stripe client init, billing-state persistence, grant/idempotency helpers, refresh-date calculation, and webhook processing.
```

---

## Summary

This Q&A log resolves the design questions raised before drafting the LLD and reflects the final product and implementation choices for the v1 billing rollout.
