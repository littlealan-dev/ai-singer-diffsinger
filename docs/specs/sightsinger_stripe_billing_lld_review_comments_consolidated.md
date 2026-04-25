# SightSinger Stripe Billing LLD — Review Comments (Consolidated)

## Overall assessment

The LLD is strong and implementation-oriented. It is aligned with the final billing spec on the major product rules and keeps a good separation between:

- Stripe billing state
- Firestore entitlement/credits state
- the existing synthesis reservation/settlement runtime

It is close to implementation-ready, but it should get a short design-fix pass before coding starts.

---

## Strengths

### 1. Scope discipline
The LLD keeps v1 focused on:

- Checkout
- Portal
- webhook handling
- scheduled refresh
- migration from legacy trial semantics

And correctly leaves out:

- self-serve plan switching
- upgrades/downgrades
- stacked subscriptions
- broader admin tooling

This is the right launch scope.

### 2. Code organization
The proposed module split is sensible:

- thin Cloud Functions entrypoints
- reusable backend modules under `src/backend`
- shared helpers for billing state, refresh, checkout, portal, webhooks

This will make testing and future migration easier.

### 3. Firestore model alignment
The design correctly reuses:

- `users/{userId}.billing`
- `users/{userId}.credits`
- top-level `credit_ledger`

instead of inventing a second independent credits model.
That is the right call given the existing Python runtime.

### 4. Webhook and scheduler split
The LLD correctly separates:

- monthly paid grants via `invoice.paid`
- free and annual refresh via scheduler

This matches the product rules and reduces ambiguity.

### 5. Legacy migration treatment
The migration logic for:

- still-active legacy trial users
- expired legacy trial users

is thoughtful and product-friendly.

### 6. Reliability foundations
The LLD includes the right building blocks for reliability:

- Stripe event idempotency
- deterministic grant ledger IDs
- non-assumption of webhook ordering
- per-user scheduler failure isolation

---

## Functional review comments

### F1. Clarify new-user bootstrap vs recurring refresh
The LLD currently initializes a brand-new user with:

- `lastCreditRefreshAt = now`
- `nextCreditRefreshAt = compute_next_monthly_refresh(now, now)`
- immediate `grant_free_monthly` ledger row

This is acceptable, but it should explicitly state:

- this is a **bootstrap grant**
- it is **not** a recurring refresh
- the first recurring free refresh happens only at `nextCreditRefreshAt`

**Recommendation**
Add a note under new-user initialization:

> The initial 8-credit grant for a brand-new user is a bootstrap grant that establishes the permanent free tier. It is not counted as a scheduler-driven recurring refresh. The first recurring free refresh occurs at `billing.nextCreditRefreshAt`.

---

### F2. Define conflict resolution when Firestore and Stripe disagree
The checkout section says:

- Firestore is the fast path
- verify with Stripe when state is ambiguous or stale

This is good, but the LLD should explicitly say what happens if they disagree.

**Recommendation**
Add an explicit rule:

> If Stripe confirms an active paid subscription, checkout must be blocked even if Firestore is stale or missing local active-plan state. Firestore should then be reconciled from Stripe-backed state before further billing actions are allowed.

---

### F3. Make migration trigger ownership explicit
The LLD describes lazy migration during login / credit access, but it does not clearly define which code path owns this trigger.

Without an explicit owner, implementation may spread migration logic across multiple flows.

**Recommendation**
Declare one authoritative entrypoint:

> `ensure_billing_state_for_login(uid, email)` is the single migration/bootstrap entrypoint for authenticated users. It should be called from the app’s authenticated startup / first credit-access path and should be responsible for initializing or repairing billing state before other recurring entitlement logic runs.

---

### F4. Strengthen monthly paid grant validation
The LLD already defines:
- one monthly paid grant per `stripeInvoiceId`

That is good.

To make this more robust, add validation that the paid invoice actually maps to a monthly plan before applying monthly webhook grant logic.

**Recommendation**
Add:

> The `invoice.paid` monthly grant path must derive `planKey` from the invoice’s recurring price and validate that the resulting plan is a monthly paid plan. Unexpected prices must be logged and skipped rather than granted.

---

### F5. Add explicit fail-fast behavior for portal configuration
The LLD requires `STRIPE_PORTAL_CONFIGURATION_ID`, which is good because the no-plan-switching policy depends on it.

However, it should explicitly say:

- do not silently fall back to Stripe default portal config

**Recommendation**
Add:

> Startup must fail fast if `STRIPE_PORTAL_CONFIGURATION_ID` is missing. The billing backend must not silently create portal sessions using Stripe’s default portal configuration.

---

### F6. Document minimum manual repair workflow
Admin tooling is intentionally out of scope, which is fine.
But some minimal operator repair expectations should still be documented.

**Recommendation**
Add a small “manual correction notes” section describing:
- which Firestore documents may be edited manually in emergency cases
- which collections are audit-only
- that Stripe remains billing source of truth
- that manual changes to credits or billing should be accompanied by a `manual_adjustment` ledger row where applicable

Suggested minimal wording:

> In emergency/manual repair scenarios, operators may inspect or correct:
> - `users/{userId}.billing`
> - `users/{userId}.credits`
> - `credit_ledger`
> - `stripe_events`
>
> Stripe remains the billing source of truth. If manual credit changes are made, write a corresponding `manual_adjustment` ledger entry.

---

### F7. Unify the safe refresh rule for `reserved > 0`
The current LLD treats scheduler-driven refreshes and monthly paid `invoice.paid` grants differently:

- scheduler refresh skips if `reserved > 0`
- monthly paid `invoice.paid` grants may still reset balance immediately while preserving reserved credits

This can work, but it adds unnecessary inconsistency.

**Recommendation**
Adopt one unified rule:

> Recurring credit resets/grants must not mutate `credits.balance` while `credits.reserved > 0`.

This rule should apply to:
- free-tier monthly refresh
- annual-plan monthly refresh
- monthly paid-plan cycle refresh after `invoice.paid`

Recommended behavior:
- `invoice.paid` should still update billing/invoice state immediately
- if `reserved == 0`, the paid monthly credit reset may be applied immediately
- if `reserved > 0`, defer the balance reset and let the scheduler retry on a later run

This gives the system one clean invariant:
- no recurring balance reset during in-flight reserved work

This reduces edge-case risk and makes the model easier to reason about.

---

## Non-functional review comments

### N1. Add transactionality requirement
The LLD implies transactions in several places, but this should be elevated to an explicit non-functional requirement.

Because login migration, webhooks, scheduler grants, and credit mutations can all interact, all state transitions that mutate both billing and credits should be transaction-safe.

**Recommendation**
Add:

> Any operation that changes recurring entitlement state and credit balance together must use Firestore transactions where possible, so that `users/{userId}.billing`, `users/{userId}.credits`, and deterministic grant/idempotency checks remain consistent under concurrent webhook, scheduler, and login flows.

---

### N2. Add structured logging requirements
The LLD talks about logging, but should specify the required structured fields.

**Recommendation**
Define required structured log fields for:

#### Checkout
- `userId`
- `planKey`
- `stripeCustomerId`
- `checkoutSessionId`

#### Webhooks
- `stripeEventId`
- `type`
- `userId`
- `stripeCustomerId`
- `stripeSubscriptionId`
- `stripeInvoiceId`

#### Scheduler
- `userId`
- `activePlanKey`
- `reserved`
- `dueAt`
- `grantType`
- result: `refreshed`, `skipped_reserved`, `failed`

This will help a lot during launch debugging and incident analysis.

---

### N3. Add Firestore index requirements
The design depends on query patterns such as:

- `billing.nextCreditRefreshAt <= now`
- possible `billing.stripeCustomerId` lookups

The LLD should explicitly state that required indexes must be provisioned before rollout.

**Recommendation**
Add a short section:

> Required Firestore indexes must be created before launch for:
> - `billing.nextCreditRefreshAt`
> - any lookup paths used by Stripe-customer-to-user resolution
>
> The scheduler must not rely on collection-wide scans.

---

### N4. Add performance expectations for webhook path
The webhook path should stay lightweight and avoid unnecessary synchronous Stripe round-trips.

This is mostly implied, but worth stating.

**Recommendation**
Add:

> Webhook handlers should avoid additional Stripe API reads unless local state is missing, ambiguous, or clearly stale. The normal fast path should process verified event payloads directly and persist Firestore state with minimal latency.

---

### N5. Clarify scheduler retry semantics and run-frequency independence
The scheduler correctly processes users independently, but the LLD should explicitly state that:

- failed/skipped users are naturally retried on future runs
- idempotency protects them from double-grants
- correctness must not depend on the job running only once per day

This matters because:
- run frequency may increase as user volume grows
- operators may trigger manual runs for urgent account repair
- the job should behave correctly no matter how many times it runs

**Recommendation**
Add:

> The refresh job must be frequency-independent. It must be safe to run:
> - on a fixed schedule
> - multiple times per day
> - manually on demand
>
> Correctness must rely on:
> - `billing.nextCreditRefreshAt` due checks
> - deterministic idempotent grant keys
> - transactional updates to billing and credits state
>
> Scheduler-driven refreshes are retry-safe by design. Users skipped due to `reserved > 0` or transient failures are retried on later runs. Deterministic grant ledger IDs must ensure that retries cannot create duplicate grants.

This makes future scaling and manual repair flows much safer.

---

### N6. Add trust boundary note for metadata
The design uses:
- Stripe customer metadata
- Checkout metadata
- `client_reference_id`

That is helpful, but the LLD should explicitly state that these are mapping hints, not the sole trust basis.

**Recommendation**
Add:

> Stripe metadata and `client_reference_id` are internal mapping aids only. Billing logic must not rely on metadata alone where verified Stripe object identity and Firestore reconciliation provide stronger grounding.

---

## Suggested priority fixes before implementation

Please address these before coding begins:

1. explicitly define **migration trigger ownership**
2. explicitly define **Stripe-vs-Firestore conflict resolution**
3. add **transactionality requirement**
4. add **structured logging requirements**
5. add **Firestore indexing requirements**
6. clarify **new-user bootstrap grant vs recurring refresh**
7. add minimal **manual repair / operator notes**
8. adopt the unified **no recurring balance reset while `reserved > 0`** rule
9. add explicit **scheduler frequency-independence** requirement

---

## Final recommendation

After the above edits, the LLD should be strong enough to proceed to implementation.

No major architecture rewrite is needed.
The document mainly needs a small number of explicit rules added so the coding agent does not have to infer operational behavior.
