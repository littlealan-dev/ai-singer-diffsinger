# SightSinger Stripe Billing LLD

## 1. Purpose

Define the low-level design for the v1 Stripe billing launch described in [sightsinger_stripe_billing_spec_v1_updated_from_qa.md](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/docs/specs/sightsinger_stripe_billing_spec_v1_updated_from_qa.md).

This LLD covers:

- the Cloud Functions for Firebase (2nd gen) billing package
- Stripe Checkout, Customer Portal, and webhook handling
- Firestore billing-state persistence
- recurring credit refresh logic for free and annual plans
- migration from legacy one-time trial behavior to permanent free tier
- integration points with the existing Python credit system in [src/backend/credits.py](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/src/backend/credits.py)

This document is for design review. It does not imply implementation has started.

## 2. Scope

Included in scope:

- new billing HTTP functions:
  - `createCheckoutSession`
  - `createPortalSession`
  - `stripeWebhook`
- new scheduled function:
  - `refreshCredits`
- shared billing helpers and Firestore persistence helpers
- migration of legacy free/trial users into recurring free-tier semantics
- billing UI backend contract for plan selection, portal launch, and displayed billing state

Not in scope:

- self-serve plan upgrades or downgrades
- parallel subscriptions
- support/admin tooling beyond minimal manual correction notes
- coupon logic other than Stripe-managed portal behavior
- usage-based Stripe metering
- changing the existing synthesis reservation/settlement workflow outside billing-specific touchpoints

## 3. Current Constraints

The design must fit the existing codebase:

- Firebase Admin helpers already exist in [src/backend/firebase_app.py](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/src/backend/firebase_app.py)
- the synthesis backend currently depends on `users/{userId}.credits` in [src/backend/credits.py](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/src/backend/credits.py)
- the legacy login path still initializes trial credits in `get_or_create_credits(...)`
- no existing billing-specific deployment package exists yet in this repo

The implementation therefore adds a new billing function entrypoint while keeping shared Firestore and credit logic in the existing `src/backend` package.

## 4. Proposed Code Layout

### 4.1 New modules

Add these modules under `src/backend`:

- `src/backend/billing_config.py`
  - loads required environment variables
  - validates Stripe IDs and URLs at startup
- `src/backend/billing_plans.py`
  - defines `PlanKey`, `PlanDefinition`, and the `PLANS` catalog
  - maps Stripe price IDs back to internal plan keys
- `src/backend/billing_auth.py`
  - parses `Authorization: Bearer <idToken>`
  - verifies Firebase ID token via existing Firebase helper
- `src/backend/billing_store.py`
  - reads/writes `users/{userId}.billing`
  - exposes transactional helpers for billing mirror updates
- `src/backend/billing_checkout.py`
  - checkout-session creation flow
  - Stripe customer lookup/create logic
- `src/backend/billing_portal.py`
  - customer portal session creation flow
- `src/backend/billing_webhooks.py`
  - top-level webhook dispatcher
  - event idempotency guard
  - event-type handlers
- `src/backend/billing_refresh.py`
  - free-tier and annual-plan refresh scheduler logic
  - next-refresh date calculation
- `src/backend/billing_migration.py`
  - legacy trial/free migration helpers
  - first-login conversion for expired legacy trial users
- `src/backend/billing_types.py`
  - typed dataclasses / `TypedDict`s for billing mirror payloads and refresh decisions

### 4.2 New function entrypoint

Add:

- `src/billing_functions/main.py`

This file exports the Cloud Functions entrypoints and keeps handler glue thin:

- `createCheckoutSession`
- `createPortalSession`
- `stripeWebhook`
- `refreshCredits`

All business logic lives in `src/backend/billing_*` modules so the same logic remains unit-testable without Cloud Functions wrappers.

### 4.3 Existing files to update

- [src/backend/credits.py](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/src/backend/credits.py)
  - remove dependence on legacy `trial_reset_v1` for recurring entitlement logic
  - add permanent-free initialization and legacy conversion hooks
- [requirements.txt](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/requirements.txt)
  - add Stripe SDK
  - add Firebase Functions Python runtime dependency if the chosen deployment packaging requires it

## 5. Environment and Startup

## 5.1 Required environment variables

Billing startup requires:

- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `STRIPE_PRODUCT_SOLO`
- `STRIPE_PRODUCT_CHOIR`
- `STRIPE_PRICE_SOLO_MONTHLY`
- `STRIPE_PRICE_SOLO_ANNUAL`
- `STRIPE_PRICE_CHOIR_EARLY_MONTHLY`
- `STRIPE_PRICE_CHOIR_EARLY_ANNUAL`
- `STRIPE_PRICE_CHOIR_MONTHLY`
- `STRIPE_PRICE_CHOIR_ANNUAL`
- `CHOIR_EARLY_SUPPORTER_ENABLED`
- `STRIPE_CHECKOUT_SUCCESS_URL`
- `STRIPE_CHECKOUT_CANCEL_URL`
- `STRIPE_PORTAL_RETURN_URL`
- `STRIPE_PORTAL_CONFIGURATION_ID`

`billing_config.py` should fail fast on cold start if any required billing variable is missing.

The billing backend must not silently fall back to Stripe defaults for required policy-bearing configuration. In particular, if `STRIPE_PORTAL_CONFIGURATION_ID` is missing, startup must fail rather than creating portal sessions with Stripe's default portal configuration.

Stripe versioning rule:

- use the latest Stripe SDK and pin the account/API behavior to the current supported API version for implementation time
- at design time, the latest Stripe API guidance available in the bundled Stripe skill is `2026-02-25.clover`
- implementation should record the chosen Stripe API version explicitly in deployment/config notes rather than relying on an untracked account-default version

## 5.2 Stripe client

`billing_config.py` should expose:

```python
@dataclass(frozen=True)
class BillingConfig:
    stripe_secret_key: str
    stripe_webhook_secret: str
    checkout_success_url: str
    checkout_cancel_url: str
    portal_return_url: str
    portal_configuration_id: str
    choir_early_supporter_enabled: bool
```

`billing_checkout.py`, `billing_portal.py`, and `billing_webhooks.py` should use a shared `get_stripe_client()` helper that caches one `stripe.StripeClient` or module-level configured client per process.

## 6. Firestore Model

## 6.1 User document

Billing uses the existing `users/{userId}` document and extends the `billing` map.

Required persisted billing fields:

```ts
type BillingState = {
  stripeCustomerId?: string;
  stripeSubscriptionId?: string;
  stripeCheckoutSessionId?: string;
  activePlanKey: PlanKey;
  stripeSubscriptionStatus?:
    | "active"
    | "past_due"
    | "canceled"
    | "incomplete"
    | "incomplete_expired"
    | "unpaid"
    | null;
  family: "free" | "solo" | "choir";
  billingInterval: "none" | "month" | "year";
  currentPeriodStart?: string | null;
  currentPeriodEnd?: string | null;
  cancelAtPeriodEnd?: boolean;
  canceledAt?: string | null;
  latestInvoiceId?: string | null;
  latestInvoicePaidAt?: string | null;
  latestInvoicePaymentFailedAt?: string | null;
  isEarlySupporter?: boolean;
  lastCreditRefreshAt?: string | null;
  nextCreditRefreshAt?: string | null;
  creditRefreshAnchor?: string | null;
  freeTierActivatedAt?: string | null;
}
```

Required `credits` fields already used by runtime:

- `balance`
- `reserved`
- `overdrafted`
- `expiresAt`
- `monthlyAllowance`
- `lastGrantType`
- `lastGrantAt`

Rules:

- `billing.activePlanKey` is the entitlement source of truth
- `credits.monthlyAllowance` is cached operational state only
- recurring billing must not depend on `credits.expiresAt`
- legacy `trialGrantedAt` and `trial_reset_v1` may remain for compatibility, but new billing decisions must not read them except in explicit migration code

## 6.2 Credit ledger

Billing writes into the existing top-level `credit_ledger`.

New grant types:

- `grant_free_monthly`
- `grant_paid_subscription_cycle`
- `grant_paid_annual_monthly_refresh`

Idempotency rules:

- monthly paid plans: exactly one grant per `stripeInvoiceId`
- scheduler refreshes: exactly one grant per `(userId, nextCreditRefreshAt_before_update, grant_type)`

Implementation choice:

- for webhook-driven monthly grants, use deterministic ledger IDs of `grant_invoice_<stripeInvoiceId>`
- for scheduler-driven grants, use deterministic ledger IDs of `grant_refresh_<userId>_<yyyymmdd_anchor_effective>`

This avoids duplicate positive grants under retry.

## 6.3 Stripe event audit

`stripe_events/{stripeEventId}` stores:

- `stripeEventId`
- `type`
- `processed`
- `processedAt`
- `relatedStripeCustomerId`
- `relatedStripeSubscriptionId`
- `relatedStripeInvoiceId`
- `relatedStripeCheckoutSessionId`
- `userId`
- `payloadSummary`
- `createdAt`

The full raw event payload is not stored in Firestore in v1.

## 7. Plan Catalog

`billing_plans.py` should expose:

```python
PlanKey = Literal[
    "free",
    "solo_monthly",
    "solo_annual",
    "choir_early_monthly",
    "choir_early_annual",
    "choir_monthly",
    "choir_annual",
]
```

Helpers:

- `get_plan(plan_key: PlanKey) -> PlanDefinition`
- `is_selectable_paid_plan(plan_key: PlanKey) -> bool`
- `get_plan_for_price_id(price_id: str) -> PlanDefinition | None`
- `get_monthly_allowance(plan_key: PlanKey) -> int`

Rules:

- `free` is never selectable through checkout
- early supporter plans are selectable only while `CHOIR_EARLY_SUPPORTER_ENABLED=true`
- Stripe price IDs must be mapped in one place only

## 8. Request Authentication

`createCheckoutSession` and `createPortalSession` require Firebase auth.

`billing_auth.py` should:

1. read `Authorization` header
2. require `Bearer <idToken>`
3. verify with `verify_id_token_claims(...)` from [src/backend/firebase_app.py](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/src/backend/firebase_app.py)
4. return:
   - `uid`
   - `email`
   - optional decoded claims if needed later

The webhook endpoint does not use Firebase auth and must only accept valid Stripe signatures.

## 9. Checkout Flow

## 9.1 Request contract

Request body:

```json
{
  "planKey": "solo_monthly"
}
```

Response:

```json
{
  "url": "https://checkout.stripe.com/..."
}
```

## 9.2 Validation

`billing_checkout.py` must validate:

- request JSON exists
- `planKey` is known
- `planKey` is selectable and paid
- no multiple-price checkout is constructed
- user is not already actively paid based on Firestore fast path
- if local state is ambiguous or stale, verify active subscriptions in Stripe before allowing checkout

Ambiguous/stale examples:

- user has `stripeCustomerId` but missing `billing.activePlanKey`
- `billing.stripeSubscriptionStatus` is absent or inconsistent with `currentPeriodEnd`
- recent webhook processing failed and left state incomplete

Conflict resolution rule:

- if Stripe confirms an active paid subscription, checkout must be blocked even if Firestore is stale, missing, or locally indicates free
- when Stripe and Firestore disagree on active paid state, Stripe wins for checkout-blocking and Firestore must be reconciled before further billing actions are allowed

## 9.3 Stripe customer mapping

Flow:

1. load `users/{uid}.billing.stripeCustomerId`
2. if present, reuse it
3. otherwise create Stripe customer with metadata:
   - `firebaseUserId`
   - `environment`
4. transactionally persist `stripeCustomerId`

The implementation must not create a new Stripe customer for every checkout retry.

## 9.4 Checkout session creation

Create a Stripe Checkout session in `subscription` mode with:

- `customer`
- one `line_item`
- `success_url`
- `cancel_url`
- `client_reference_id = uid`
- metadata:
  - `firebaseUserId`
  - `planKey`

After session creation, store:

- `billing.stripeCheckoutSessionId`

This field is advisory only and must not be used as the subscription source of truth.

## 9.5 Checkout return sync fallback

Stripe webhooks remain the primary billing update path. The app may also expose an authenticated checkout-return reconciliation endpoint for hosted Checkout success redirects:

- `POST /billing/checkout-session/sync`

Request body:

```json
{
  "sessionId": "cs_test_..."
}
```

Response:

```json
{
  "synced": true,
  "status": "complete",
  "activePlanKey": "solo_annual"
}
```

Purpose:

- repair local development when Stripe webhook forwarding is not running
- reduce user-visible waiting after a successful hosted Checkout redirect
- reconcile Firestore from verified Stripe state if webhook delivery is delayed

Rules:

- require Firebase auth
- retrieve the Checkout Session from Stripe by `sessionId`
- verify the retrieved session belongs to the authenticated user using `client_reference_id` / `metadata.firebaseUserId`
- require a stored Firestore linkage for the authenticated user:
  - `billing.stripeCheckoutSessionId` must match the retrieved Checkout Session, or
  - `billing.stripeCustomerId` must match the retrieved Checkout Session customer
- reject metadata-only matches that have no stored checkout/customer linkage
- reject non-subscription Checkout Sessions
- reject sessions that are not `complete` or whose payment status is not paid / no-payment-required
- reject subscriptions whose current Stripe status is not `active`, `trialing`, or `past_due`
- retrieve or expand the subscription and map its recurring price through the shared plan catalog
- use Stripe subscription and invoice data as the source of truth for Firestore reconciliation
- do not create a new Stripe customer or user from this path
- keep webhook idempotency semantics by using the same deterministic invoice grant key, `grant_invoice_<stripeInvoiceId>`, whenever the endpoint applies a paid-cycle grant

This endpoint is a backend repair/reconciliation path. The success page must not grant access locally; it may call this endpoint and then continue to observe Firestore billing state.

## 10. Portal Flow

## 10.1 Preconditions

Portal launch requires:

- authenticated user
- existing `billing.stripeCustomerId`

If `stripeCustomerId` is missing, return `409` instead of silently creating a Stripe customer.

## 10.2 Session creation

Create Stripe Billing Portal session with:

- `customer = stripeCustomerId`
- `return_url = STRIPE_PORTAL_RETURN_URL`
- `configuration = STRIPE_PORTAL_CONFIGURATION_ID`

Return:

```json
{
  "url": "https://billing.stripe.com/..."
}
```

Portal configuration rule:

- create a dedicated Stripe Billing Portal configuration for v1
- enable payment method updates
- enable cancellation at period end
- disable subscription price/product switching

The backend should pass the explicit portal configuration ID when creating sessions so the v1 “no self-serve plan switching” rule is enforced by configuration, not only by UI convention.

## 10.3 Portal return sync fallback

Stripe webhooks remain the primary source of subscription changes from Billing Portal. The app may also expose an authenticated portal-return reconciliation endpoint:

- `POST /billing/subscription/sync`

Response:

```json
{
  "synced": true,
  "status": "active",
  "activePlanKey": "solo_monthly"
}
```

Purpose:

- repair local development when Stripe webhook forwarding is not running
- reduce user-visible delay after Billing Portal actions
- reconcile Firestore from Stripe after cancellation, cancellation-at-period-end, payment-status changes, or portal-driven subscription updates

Rules:

- require Firebase auth
- require existing `billing.stripeCustomerId`; do not create a Stripe customer from this path
- list the authenticated user's Stripe subscriptions for that customer using server-side Stripe credentials
- prefer the currently stored `billing.stripeSubscriptionId` when present; otherwise prefer a paid/status-relevant subscription
- for `active`, `trialing`, `past_due`, `unpaid`, or `incomplete`, map the subscription price through the shared plan catalog and update the billing mirror
- for `canceled` or `incomplete_expired`, revert the billing mirror to free while preserving the current credit refresh anchor
- for `cancel_at_period_end=true`, keep paid entitlement active and set `billing.cancelAtPeriodEnd=true`
- reject unknown subscription prices or unsupported statuses rather than guessing

`STRIPE_PORTAL_RETURN_URL` should include a UI signal such as `?billing=sync` so the app can call this endpoint after returning from Billing Portal and then continue observing Firestore billing state.

## 11. Webhook Processing

## 11.1 HTTP handling

`stripeWebhook` must:

1. read the raw request body bytes
2. read `Stripe-Signature`
3. construct and verify the event using `STRIPE_WEBHOOK_SECRET`
4. reject invalid signatures with `400`
5. dispatch the verified event to `billing_webhooks.handle_event(event)`

## 11.2 Event idempotency

Before processing business logic:

1. check `stripe_events/{event.id}`
2. if `processed == true`, return `200` immediately
3. otherwise create/update the audit record as processing started

After successful handler completion:

- set `processed = true`
- set `processedAt`

If handler logic fails:

- leave `processed = false`
- keep summary fields for debugging
- return non-2xx so Stripe retries

Webhook ordering rule:

- handlers must not depend on webhook delivery order
- when correctness depends on current subscription state, the handler should treat Stripe as source of truth and reconcile against the latest known subscription/invoice state instead of assuming the event stream arrives in sequence

Webhook fast-path rule:

- webhook handlers should avoid additional Stripe API reads unless local state is missing, ambiguous, or clearly stale
- the normal fast path should process verified event payloads directly and persist Firestore state with minimal latency

## 11.3 Event-to-handler mapping

Handlers:

- `checkout.session.completed`
  - persist Stripe customer and subscription references
  - initialize billing mirror if needed
  - do not grant monthly paid credits here
- `invoice.paid`
  - update invoice mirror fields
  - for monthly paid plans, grant exactly once per `stripeInvoiceId`
  - for annual plans, update paid state but do not perform monthly scheduler-style refresh here
- `invoice.payment_failed`
  - update `latestInvoicePaymentFailedAt`
  - keep plan paid until Stripe subscription state later becomes non-active
- `customer.subscription.updated`
  - update:
    - `activePlanKey`
    - `stripeSubscriptionStatus`
    - `currentPeriodStart`
    - `currentPeriodEnd`
    - `cancelAtPeriodEnd`
    - `canceledAt`
  - preserve the current `creditRefreshAnchor` unless a separate paid-grant path re-anchors it
- `customer.subscription.deleted`
  - revert billing mirror to free
  - preserve current anchor
  - recompute `nextCreditRefreshAt` from that anchor using free allowance semantics

Implementation preference for subscription lifecycle handlers:

- for `customer.subscription.updated` and `customer.subscription.deleted`, use the subscription object from the verified event as the primary payload
- if the local billing mirror is missing critical identifiers or appears stale/inconsistent, retrieve the latest subscription from Stripe before finalizing the Firestore mirror update

## 11.4 Stripe-to-user lookup

Lookup order:

1. if `client_reference_id` or metadata contains `firebaseUserId`, use it
2. otherwise look up by `stripeCustomerId` in Firestore
3. if neither resolves, fail handler and rely on retry / manual repair

The system should not create a new user record from webhook-only Stripe data.

## 11.5 Monthly paid grant algorithm

For `invoice.paid` on monthly plans:

1. identify `userId`
2. identify `planKey` from invoice line-item price
3. validate that `planKey` is a monthly paid plan
4. if the invoice price does not map to an expected monthly paid plan, log and skip grant logic
5. start Firestore transaction
6. check if ledger entry `grant_invoice_<invoiceId>` already exists
7. if yes, treat as idempotent success
8. otherwise update billing/invoice mirror state immediately
9. if `credits.reserved > 0`, do not reset `credits.balance` yet
10. if `credits.reserved == 0`, apply the recurring paid reset immediately
11. in both cases, reuse the existing deferred-refresh mechanism rather than introducing a separate monthly-paid deferral path

Detailed mutation rules:

- always update billing state immediately on `invoice.paid`
- never mutate `credits.balance` for recurring resets while `credits.reserved > 0`
- recurring credits are non-rollover resets, so delayed application results in one reset to the current entitled allowance, not stacked missed-cycle grants

If `credits.reserved == 0`, the transaction should:

- set `credits.balance = monthly_allowance`
- leave `credits.reserved` unchanged
- set `credits.monthlyAllowance`
- set `credits.lastGrantType = "grant_paid_subscription_cycle"`
- set `credits.lastGrantAt = now`
- if this invoice is the first successful paid grant after free, set `billing.creditRefreshAnchor = now`
- set `billing.lastCreditRefreshAt = now`
- compute and persist next `billing.nextCreditRefreshAt`
- write deterministic grant ledger row

If `credits.reserved > 0`, the transaction should:

- update invoice/billing mirror fields immediately
- leave `credits.balance` unchanged
- do not move `billing.creditRefreshAnchor` yet
- leave the user in a due/deferred state using the existing refresh mechanism so the scheduler can retry later
- not write the positive monthly grant ledger row until the reset is actually applied

Deferred anchor-move rule:

- if entering paid from free and the initial paid reset is deferred because `reserved > 0`, the anchor must move only when the deferred paid reset is actually applied
- webhook retries must not duplicate either the paid reset or the anchor move

## 11.6 Checkout session subscription options

When creating Checkout Sessions in `subscription` mode:

- use Stripe Checkout, not manual PaymentIntent subscription flows
- pass only one recurring price for the v1 purchase path
- include `success_url` with `{CHECKOUT_SESSION_ID}`
- include `client_reference_id` and metadata for internal user mapping

Recommended enhancement:

- evaluate setting Checkout subscription billing mode to `flexible` when implementing, because current Stripe subscription guidance recommends it for newer integrations on supported API versions

This is a recommendation, not a v1 product requirement. If adopted, it should be validated in Stripe test mode before launch.

## 12. Refresh Anchor and Date Calculation

## 12.1 Active anchor rule

Every user has one active `billing.creditRefreshAnchor` field representing the current monthly credit cadence.

Meaning:

- anchor determines when recurring monthly refresh happens
- current effective entitlement determines how much to grant

Anchor transition rules:

- free-tier bootstrap initializes the anchor
- first successful paid grant re-anchors the user on free -> paid entry
- returning to free preserves the current anchor
- later re-subscribe from free re-anchors again on the new paid start date

## 12.2 Anchor initialization

Initialization rules:

- new permanent free user:
  - anchor = first permanent free-tier bootstrap / grant date
- legacy still-active trial user at launch:
  - anchor = original registration / trial cadence
- legacy expired trial user at launch:
  - anchor = first post-launch free-tier conversion login
- currently-free former paid user:
  - preserve existing anchor if already present
  - otherwise initialize from account creation time
- free -> paid entry:
  - anchor = first successful paid grant date
- paid -> free:
  - preserve current anchor
- free again -> paid again later:
  - re-anchor again on that new first successful paid grant date
- active paid user with no anchor:
  - initialize from the first successful paid grant date when possible
  - otherwise use account creation time as migration-repair default

## 12.3 Date calculation helper

`billing_refresh.py` should expose:

- `compute_next_monthly_refresh(anchor: datetime, after: datetime) -> datetime`

Rules:

- monthly calendar cadence, not rolling 30 days
- use anchor day-of-month when possible
- if the anchor day does not exist in a target month, use the last day of that month
- preserve time-of-day from the anchor in UTC

## 13. Scheduler Design

## 13.1 Due query

The scheduler queries only:

- `users` where `billing.nextCreditRefreshAt <= now`

Anchor/date recomputation is used only for:

- user initialization
- migration
- explicit repair

The scheduler must not scan all users and recompute due-ness from scratch on every run.

## 13.2 Eligible users

The scheduler handles:

- free plan users
- annual paid users
- users whose prior due refresh was skipped because `reserved > 0`

It does not normally grant monthly paid cycles; those are handled by `invoice.paid`.

## 13.3 Refresh algorithm

For each due user:

1. read current billing and credits state
2. determine effective entitlement:
   - if active annual paid subscription exists, use annual plan allowance
   - otherwise if no active paid entitlement, use free allowance
   - monthly paid plans are only repair/reconciliation candidates here
3. if `credits.reserved > 0`:
   - skip mutation
   - leave `nextCreditRefreshAt` unchanged
   - retry on next daily run
4. if `credits.reserved == 0`:
   - transactionally set `credits.balance = allowance`
   - leave `credits.reserved` unchanged
   - set `credits.monthlyAllowance = allowance`
   - set `credits.lastGrantType`
   - set `credits.lastGrantAt = now`
   - set `billing.lastCreditRefreshAt = now`
   - compute and persist new `billing.nextCreditRefreshAt`
   - write deterministic grant ledger row

Grant types:

- free users: `grant_free_monthly`
- annual paid users: `grant_paid_annual_monthly_refresh`
- deferred monthly paid resets after `invoice.paid`: `grant_paid_subscription_cycle`

Unified safe-reset invariant:

- recurring credit resets must not mutate `credits.balance` while `credits.reserved > 0`
- this invariant applies to free-tier monthly refresh, annual-plan monthly refresh, and monthly paid-plan cycle resets
- delayed application always resolves to one reset to the user's current entitled allowance, never stacked missed-cycle grants

Annual-plan anchor rule:

- first successful annual `invoice.paid` establishes the paid anchor when entering annual from free
- subsequent annual-plan monthly credits are scheduler-driven from that paid-start anchor

## 13.4 Repair mode

The scheduler may repair monthly-paid refresh state only when explicitly enabled in code for reconciliation cases.

Default v1 behavior:

- monthly paid refreshes come from `invoice.paid`
- scheduler repair path is not part of the normal grant path

## 14. Migration and First-Login Conversion

## 14.1 Legacy login path replacement

`get_or_create_credits(...)` in [src/backend/credits.py](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/src/backend/credits.py) currently grants a 20-credit expiring trial and performs the one-time `trial_reset_v1`.

That behavior must be replaced with:

- permanent free-tier initialization for truly new users
- legacy migration-aware conversion for old users
- no new recurring entitlement decision based on `trial_reset_v1`

## 14.2 New user initialization

For a brand-new user with no `credits` and no `billing`:

transactionally create:

- `billing.activePlanKey = "free"`
- `billing.stripeSubscriptionStatus = null`
- `billing.family = "free"`
- `billing.billingInterval = "none"`
- `billing.creditRefreshAnchor = now`
- `billing.lastCreditRefreshAt = now`
- `billing.nextCreditRefreshAt = compute_next_monthly_refresh(now, now)`
- `billing.freeTierActivatedAt = now`
- `credits.balance = 8`
- `credits.reserved = 0`
- `credits.overdrafted = false`
- `credits.expiresAt = null`
- `credits.monthlyAllowance = 8`
- `credits.lastGrantType = "grant_free_monthly"`
- `credits.lastGrantAt = now`
- one `grant_free_monthly` ledger row

This replaces the old 20-credit / 30-day trial bootstrap.

Bootstrap note:

- the initial 8-credit grant for a brand-new user is a bootstrap grant that establishes the permanent free tier
- it is not counted as a scheduler-driven recurring refresh
- the first recurring free refresh occurs only at `billing.nextCreditRefreshAt`

## 14.3 Existing user migration helper

Add `ensure_billing_state_for_login(uid, email)` in `billing_migration.py`.

Responsibilities:

- guarantee `users/{uid}.billing` exists
- detect whether legacy trial logic still owns the account
- perform one-time free-tier conversion where required
- preserve balance until the next due refresh for legacy non-paying users

This is the single authoritative migration/bootstrap entrypoint for authenticated users. It should be called from the authenticated startup / first credit-access path before other recurring entitlement logic runs.

## 14.4 Migration cases

At launch of Stripe paid plans, the pre-launch user base contains legacy trial users only. There is no previously launched Stripe paid-plan cohort to migrate.

Case A: legacy still-active trial user

- preserve current credits
- preserve original cadence from registration / trial start
- do not immediately clamp to 8

Case B: legacy expired trial user

- on first login after launch:
  - convert to free
  - grant 8 immediately
  - set anchor to conversion time
  - set `billing.freeTierActivatedAt`
  - write ledger grant
- this conversion must be one-time only

Normal happy-path anchor examples:

- free -> monthly with `reserved == 0`:
  - first successful `invoice.paid` grants immediately and becomes the new anchor
- free -> monthly with `reserved > 0`:
  - billing state updates immediately
  - paid reset and anchor move are deferred until scheduler application
- free -> annual:
  - first successful `invoice.paid` grants immediately and becomes the new anchor
  - later annual monthly refreshes follow that paid-start anchor
- paid -> free:
  - keep the current anchor
- free again -> paid again later:
  - first successful paid grant on the new paid start becomes the new anchor

## 14.5 Migration trigger

V1 should not require a one-shot offline backfill before launch.

Primary migration path:

- lazy migration during authenticated user login / credit access

Secondary repair path:

- scheduler and webhook handlers backfill missing billing fields when the user appears in their flows

## 15. Interaction With Existing Credit Runtime

## 15.1 Reservation and settlement

The synthesis runtime continues to use:

- `reserve_credits(...)`
- `settle_credits(...)`
- `settle_credits_and_complete_job(...)`
- `release_credits(...)`

Billing changes must not rewrite that reservation model.

## 15.2 Required changes in `credits.py`

`credits.py` must be updated so:

- `get_or_create_credits(...)` no longer grants 20 expiring trial credits to new users
- recurring entitlement initialization uses the new billing-aware path
- `expiresAt` can remain populated for legacy users but recurring logic ignores it

The reservation/settlement functions themselves do not need Stripe knowledge.

## 16. Failure Handling

## 16.1 Checkout

Return classes:

- `400` invalid request body / invalid plan key
- `401` missing or invalid auth
- `409` already has active paid subscription
- `409` missing or ambiguous state that fails Stripe verification
- `500` Stripe or Firestore infrastructure failure

## 16.2 Webhook

If a handler cannot resolve user identity or persist state:

- return non-2xx
- rely on Stripe retry
- log event id, type, customer id, invoice id, subscription id

## 16.3 Scheduler

Per-user failures must not abort the whole scheduled run.

Required behavior:

- process users independently
- log structured failure per user
- continue remaining users
- expose summary counts:
  - scanned
  - refreshed
  - skipped_reserved
  - failed

The refresh job must be frequency-independent. It must be safe to run:

- on a fixed schedule
- multiple times per day
- manually on demand

Correctness must rely on:

- `billing.nextCreditRefreshAt` due checks
- deterministic idempotent grant keys
- transactional updates to billing and credits state

Scheduler-driven refreshes are retry-safe by design. Users skipped due to `reserved > 0` or transient failures are retried on later runs. Deterministic grant ledger IDs must ensure retries cannot create duplicate grants.

## 16.4 Transactionality and trust boundaries

Any operation that changes recurring entitlement state and credit balance together must use Firestore transactions where possible, so that `users/{userId}.billing`, `users/{userId}.credits`, and deterministic grant/idempotency checks remain consistent under concurrent webhook, scheduler, and login flows.

Stripe metadata and `client_reference_id` are internal mapping aids only. Billing logic must not rely on metadata alone where verified Stripe object identity and Firestore reconciliation provide stronger grounding.

## 16.5 Structured logging

Required structured log fields:

Checkout:

- `userId`
- `planKey`
- `stripeCustomerId`
- `checkoutSessionId`

Webhooks:

- `stripeEventId`
- `type`
- `userId`
- `stripeCustomerId`
- `stripeSubscriptionId`
- `stripeInvoiceId`

Scheduler:

- `userId`
- `activePlanKey`
- `reserved`
- `dueAt`
- `grantType`
- `result`

## 16.6 Firestore indexes

Required Firestore indexes must be created before launch for:

- `billing.nextCreditRefreshAt`
- any lookup paths used by Stripe-customer-to-user resolution, including `billing.stripeCustomerId`

The scheduler must not rely on collection-wide scans.

## 16.7 Manual correction notes

In emergency/manual repair scenarios, operators may inspect or correct:

- `users/{userId}.billing`
- `users/{userId}.credits`
- `credit_ledger`
- `stripe_events`

Stripe remains the billing source of truth. If manual credit changes are made, write a corresponding `manual_adjustment` ledger entry.

## 17. Testing Plan

## 17.1 Unit tests

Add focused tests for:

- plan catalog selection and early-supporter gating
- next-refresh date calculation including month-end anchors
- webhook invoice idempotency by `stripeInvoiceId`
- scheduler idempotency by deterministic ledger ID
- login migration cases A-D
- first-login conversion of expired legacy trials
- currently-free former paid users preserving anchor
- free -> monthly immediate paid grant re-anchor
- free -> monthly deferred paid grant because `reserved > 0`
- free -> annual first paid grant sets anchor
- annual monthly refresh follows paid-start anchor
- paid -> free keeps current anchor
- free again -> paid again re-anchors
- webhook retry cannot duplicate grant or anchor move

## 17.2 Integration tests

Add Firestore-backed integration tests for:

- checkout creating one Stripe customer per Firebase user
- checkout blocking second paid subscription
- authenticated checkout-return sync accepting only the owning user's completed Checkout Session
- portal session requiring existing `stripeCustomerId`
- portal return sync reflecting cancellation-at-period-end and immediate cancellation
- webhook state transitions for:
  - `checkout.session.completed`
  - `invoice.paid`
  - `invoice.payment_failed`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`
- scheduler skipping while `reserved > 0` and succeeding later

## 17.3 Existing test updates

Update [tests/test_credits.py](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/tests/test_credits.py) so legacy trial-reset tests are replaced by:

- new-user permanent free-tier bootstrap
- expired-legacy-trial conversion on first login
- still-active legacy trial preservation until natural boundary
- free -> monthly paid-entry re-anchor
- free -> annual paid-entry re-anchor
- deferred paid reset applies anchor move only when the reset is actually applied

## 18. Rollout Order

Recommended order:

1. add billing config, plan catalog, and types
2. add billing store and refresh-date helpers
3. replace legacy new-user credit bootstrap in `credits.py`
4. implement login migration helper
5. implement checkout and portal endpoints
6. implement webhook dispatcher and handlers
7. implement scheduler refresh logic
8. add unit and integration coverage
9. validate end-to-end in Stripe test mode
10. deploy soft launch

## 19. Open Items

Still intentionally deferred from this LLD:

- support/admin manual adjustment workflow UX
- automated reconciliation dashboarding
- future custom upgrade/downgrade path
