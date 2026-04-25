# SightSinger Stripe Billing Integration Spec (v1, Final Updated from Q&A)

## Overview

This document defines the v1 paid subscription implementation for SightSinger using **Stripe Checkout + Stripe Customer Portal + webhooks**.

Goals:

- Launch paid plans with minimal billing complexity
- Keep credits and entitlements managed inside SightSinger
- Use Stripe for payment collection, receipts, invoices, and subscription self-service
- Avoid self-serve plan switching in v1
- Support annual billing while still refreshing credits monthly
- Implement billing on Cloud Functions for Firebase (2nd gen) instead of the GPU Cloud Run synthesis backend

Out of scope for v1:

- Usage-based billing in Stripe
- Self-serve plan upgrades/downgrades
- Coupon campaigns except Stripe portal retention coupons
- Multi-currency pricing
- Tax ID / VAT handling beyond basic billing details
- Enterprise/custom invoicing flows

---

## Launch Pricing Model

### Free
- 8 credits per month
- 1 credit = approximately 30 seconds of audio

Migration note:
- the current backend still contains legacy one-time trial behavior
- v1 billing launch should treat that behavior as deprecated
- the permanent free plan becomes:
  - **8 credits per month**
  - no 30-day trial expiry
  - no one-time 20-credit grant model going forward

### Solo
- Monthly: **$8.99/month**
- Annual: **$89/year**
- Credits: **30 credits/month**

### Choir
Standard:
- Monthly: **$24.99/month**
- Annual: **$249/year**
- Credits: **120 credits/month**

Early supporter:
- Monthly: **$19.99/month**
- Annual: **$199/year**
- Credits: **120 credits/month**

---

## Stripe Product and Price IDs

### Products
- `solo_product`: `prod_UNZ9giszEmhknb`
- `choir_product`: `prod_UNZFnQYPSSgGBx`

### Prices
- `solo_monthly`: `price_1TOo18BTGetqdQsGCJqvxNTK`
- `solo_annual`: `price_1TOo18BTGetqdQsGtgfDWKz8`
- `choir_early_monthly`: `price_1TOo6wBTGetqdQsGfYDgHJM1`
- `choir_early_annual`: `price_1TOo6wBTGetqdQsGdvgaAboF`
- `choir_monthly`: `price_1TOo6wBTGetqdQsGIgWkYsgU`
- `choir_annual`: `price_1TOo6wBTGetqdQsGcLP3GvIG`

---

## Business Rules

### Plan switching
v1 does **not** support self-serve plan switching.

Allowed in v1:
- buy a subscription
- update payment method
- view invoices
- cancel subscription at period end

Not allowed in v1:
- self-serve upgrade from Solo to Choir
- self-serve downgrade from Choir to Solo
- switching between monthly and annual in the portal
- custom upgrade flow in initial launch scope

Future versions may add custom upgrade/downgrade flows later.

### No parallel subscriptions
v1 does **not** allow parallel or stacked subscriptions.

If a user already has an active paid subscription:
- do not create a second checkout session
- do not allow stacking a second subscription on top of the first
- direct the user to **Manage Billing** or support
- custom upgrade/downgrade flows may be added later, but are out of scope for the initial launch

### Cancellation
- Cancellation is **at end of billing period**
- User keeps paid access until current paid period ends
- User keeps credits already granted for the active billing cycle unless manually adjusted by admin later

### Failed payments
v1 behavior:
- Stripe remains source of truth for payment state
- SightSinger listens for `invoice.payment_failed`
- User should be notified to update payment method
- Paid entitlement can remain until Stripe subscription status changes to a non-active state, depending on webhook state handling

### Annual plans
Annual subscribers are billed yearly by Stripe, but still receive:
- monthly credit refresh
- the same monthly allowance as monthly subscribers of the same plan

This requires SightSinger to run its own monthly credit refresh scheduler for annual subscribers.

### Early supporter pricing
Early supporter Choir prices are controlled by a config flag in SightSinger.

- `CHOIR_EARLY_SUPPORTER_ENABLED=true` means the UI/backend may offer early supporter Choir prices
- when set to `false`, the app stops offering early supporter prices
- existing subscribers on early supporter prices remain on those Stripe subscriptions unless manually migrated later
- the decision to turn the flag off is manual and will be based on product analytics/business judgment

---

## Architecture

### Deployment decision
Billing will be implemented on **Cloud Functions for Firebase (2nd gen)**, not on:
- the GPU-enabled Cloud Run synthesis backend
- the marketing Next.js server-side runtime

### Rationale for Cloud Functions 2nd gen
This is the chosen middle ground between:
- embedding billing endpoints inside the marketing Next.js app, and
- creating a completely separate standard Cloud Run billing service

Reasons for choosing Cloud Functions 2nd gen:

1. **Much cheaper and more appropriate than the GPU backend**
   - billing traffic is lightweight, bursty, and latency-sensitive
   - GPU Cloud Run is designed for audio synthesis and heavy compute, not webhooks and small JSON APIs
   - the GPU service has cold starts and higher running cost

2. **Good fit for Stripe billing patterns**
   - HTTPS handlers are suitable for:
     - create checkout session
     - create portal session
     - Stripe webhook
   - scheduled functions are suitable for:
     - monthly credit refresh
     - periodic billing reconciliation jobs

3. **Lower operational overhead than a separate custom service**
   - remains inside the Firebase/GCP ecosystem already used by SightSinger
   - easy access to Firestore and Secret Manager
   - simpler deployment and maintenance for a solo founder setup

4. **Cleaner separation than using the marketing Next.js runtime**
   - billing is an application/business backend concern, not a marketing-site concern
   - avoids coupling Stripe webhooks and billing logic to marketing-site deploys and SEO/public-web concerns

5. **Enough flexibility for v1**
   - supports HTTP endpoints, scheduled jobs, secrets, scaling controls, and logging
   - if billing grows significantly later, it can still be extracted to a separate Cloud Run service

### Non-goals of this decision
This does **not** mean Firebase Analytics-triggered functions will be used.

The limitation that Cloud Functions for Firebase (2nd gen) does not support Analytics event triggers is not important for this billing design because billing is driven by:
- authenticated HTTPS requests
- Stripe webhooks
- scheduled refresh jobs

### Cloud Functions endpoint shape
Billing will use **separate Cloud Functions for Firebase (2nd gen) HTTP endpoints**, not a single multi-route billing app.

HTTP functions:
- `createCheckoutSession`
- `createPortalSession`
- `stripeWebhook`

Scheduled functions:
- `refreshCredits`

Rationale:
- keeps Stripe webhook handling isolated
- reduces routing and middleware complexity
- makes logging, testing, and failure isolation simpler for v1

### Runtime layout

#### Marketing landing page
- **Firebase App Hosting**
- **Next.js**
- responsibility:
  - public website
  - SEO pages
  - pricing display
  - non-sensitive marketing content

#### App UI
- **Firebase Hosting**
- **React**
- responsibility:
  - authenticated product UI
  - account page
  - billing buttons
  - credit display
  - upgrade prompts

#### Billing backend
- **Cloud Functions for Firebase (2nd gen)**
- responsibility:
  - create checkout session
  - create customer portal session
  - receive Stripe webhooks
  - update Firestore billing mirror
  - run scheduled monthly credit refresh

#### Synthesis backend
- **GCP Cloud Run on GPU-enabled serverless container**
- **Python**
- responsibility:
  - score processing
  - AI singing synthesis
  - heavy compute/audio generation only

#### Shared data layer
- **Firestore**
- responsibility:
  - billing mirror
  - credits state
  - credit ledger
  - customer-to-user mapping
  - entitlement state consumed by both app UI and synthesis backend

### Responsibility boundaries

#### Stripe handles
- payment collection
- Checkout
- Customer Portal
- invoices
- receipts
- recurring billing lifecycle
- payment method management
- cancellation requests

#### Cloud Functions billing layer handles
- authenticated billing API endpoints
- Stripe webhook signature verification
- mapping Firebase user to Stripe customer
- syncing Stripe billing state into Firestore
- monthly credit refresh scheduling
- internal billing audit trail

#### SightSinger app handles
- user authentication
- internal plan keys
- credits and entitlements
- product analytics
- displaying billing state to users

#### GPU backend handles
- generation authorization checks against Firestore credit/entitlement state
- consumption of credits during synthesis workflows
- no Stripe logic

### Core integration pattern
1. User signs in to SightSinger
2. User selects a paid plan
3. Frontend sends selected `planKey` to billing backend
4. Billing backend creates Stripe Checkout Session for exactly one Stripe Price ID
5. User completes payment on Stripe Checkout
6. Stripe sends webhook events to the billing backend
7. Billing backend persists billing state to Firestore
8. Billing backend grants or schedules credits
9. User can later open Stripe Customer Portal from SightSinger account page
10. Synthesis backend reads entitlement and credits from Firestore before allowing generation

### Why not use the GPU Cloud Run backend
The GPU backend should not host billing APIs because:
- higher cost per request path
- slow cold starts
- wrong runtime profile for webhook traffic
- unnecessary coupling of billing reliability to synthesis infrastructure

### Why not use the marketing Next.js runtime for billing
It is technically possible, but not selected for v1 because:
- billing is not a marketing concern
- Stripe webhook handling should be isolated from landing-page deployments
- billing will likely grow into a more app-backend-oriented concern than a public-web concern

### Future migration path
If billing complexity grows later, Cloud Functions 2nd gen can be migrated to:
- a separate standard Cloud Run billing service
without changing the product catalog, billing rules, or Firestore billing model.

---

## Plan Keys

The app should use internal plan keys rather than hardcoding Stripe IDs in multiple places.

```ts
export type PlanKey =
  | "free"
  | "solo_monthly"
  | "solo_annual"
  | "choir_early_monthly"
  | "choir_early_annual"
  | "choir_monthly"
  | "choir_annual";
```

### Plan metadata model

```ts
type PlanDefinition = {
  planKey: PlanKey;
  stripePriceId?: string;
  stripeProductId?: string;
  displayName: string;
  family: "free" | "solo" | "choir";
  billingInterval: "none" | "month" | "year";
  creditsPerMonth: number;
  isEarlySupporter?: boolean;
  isSelectable: boolean;
};
```

### Recommended single source of truth

```ts
const PLANS: Record<PlanKey, PlanDefinition> = {
  free: {
    planKey: "free",
    displayName: "Free",
    family: "free",
    billingInterval: "none",
    creditsPerMonth: 8,
    isSelectable: false,
  },
  solo_monthly: {
    planKey: "solo_monthly",
    stripePriceId: process.env.STRIPE_PRICE_SOLO_MONTHLY!,
    stripeProductId: process.env.STRIPE_PRODUCT_SOLO!,
    displayName: "Solo Monthly",
    family: "solo",
    billingInterval: "month",
    creditsPerMonth: 30,
    isSelectable: true,
  },
  solo_annual: {
    planKey: "solo_annual",
    stripePriceId: process.env.STRIPE_PRICE_SOLO_ANNUAL!,
    stripeProductId: process.env.STRIPE_PRODUCT_SOLO!,
    displayName: "Solo Annual",
    family: "solo",
    billingInterval: "year",
    creditsPerMonth: 30,
    isSelectable: true,
  },
  choir_early_monthly: {
    planKey: "choir_early_monthly",
    stripePriceId: process.env.STRIPE_PRICE_CHOIR_EARLY_MONTHLY!,
    stripeProductId: process.env.STRIPE_PRODUCT_CHOIR!,
    displayName: "Choir Early Monthly",
    family: "choir",
    billingInterval: "month",
    creditsPerMonth: 120,
    isEarlySupporter: true,
    isSelectable: process.env.CHOIR_EARLY_SUPPORTER_ENABLED === "true",
  },
  choir_early_annual: {
    planKey: "choir_early_annual",
    stripePriceId: process.env.STRIPE_PRICE_CHOIR_EARLY_ANNUAL!,
    stripeProductId: process.env.STRIPE_PRODUCT_CHOIR!,
    displayName: "Choir Early Annual",
    family: "choir",
    billingInterval: "year",
    creditsPerMonth: 120,
    isEarlySupporter: true,
    isSelectable: process.env.CHOIR_EARLY_SUPPORTER_ENABLED === "true",
  },
  choir_monthly: {
    planKey: "choir_monthly",
    stripePriceId: process.env.STRIPE_PRICE_CHOIR_MONTHLY!,
    stripeProductId: process.env.STRIPE_PRODUCT_CHOIR!,
    displayName: "Choir Monthly",
    family: "choir",
    billingInterval: "month",
    creditsPerMonth: 120,
    isSelectable: true,
  },
  choir_annual: {
    planKey: "choir_annual",
    stripePriceId: process.env.STRIPE_PRICE_CHOIR_ANNUAL!,
    stripeProductId: process.env.STRIPE_PRODUCT_CHOIR!,
    displayName: "Choir Annual",
    family: "choir",
    billingInterval: "year",
    creditsPerMonth: 120,
    isSelectable: true,
  },
};
```

---

## Environment Variables

```bash
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=

STRIPE_PRODUCT_SOLO=prod_UNZ9giszEmhknb
STRIPE_PRODUCT_CHOIR=prod_UNZFnQYPSSgGBx

STRIPE_PRICE_SOLO_MONTHLY=price_1TOo18BTGetqdQsGCJqvxNTK
STRIPE_PRICE_SOLO_ANNUAL=price_1TOo18BTGetqdQsGtgfDWKz8

STRIPE_PRICE_CHOIR_EARLY_MONTHLY=price_1TOo6wBTGetqdQsGfYDgHJM1
STRIPE_PRICE_CHOIR_EARLY_ANNUAL=price_1TOo6wBTGetqdQsGdvgaAboF

STRIPE_PRICE_CHOIR_MONTHLY=price_1TOo6wBTGetqdQsGIgWkYsgU
STRIPE_PRICE_CHOIR_ANNUAL=price_1TOo6wBTGetqdQsGcLP3GvIG

CHOIR_EARLY_SUPPORTER_ENABLED=true

FREE_CREDITS_PER_MONTH=8
SOLO_CREDITS_PER_MONTH=30
CHOIR_CREDITS_PER_MONTH=120

APP_BASE_URL=https://sightsinger.app
STRIPE_CHECKOUT_SUCCESS_URL=https://sightsinger.app/app/billing/success?session_id={CHECKOUT_SESSION_ID}
STRIPE_CHECKOUT_CANCEL_URL=https://sightsinger.app/pricing?checkout=cancelled
STRIPE_PORTAL_RETURN_URL=https://sightsinger.app/app/settings/billing
```

### Test and live mode separation
Stripe test mode and live mode must use separate:
- secret keys
- webhook secrets
- environment configuration values

Do not mix test and live billing credentials in the same deployment environment.

---

## Firestore Data Model

Recommended collections / documents, aligned to the current implementation:

- `users/{userId}`
- `credit_reservations/{jobId}`
- `credit_ledger/{entryId}`
- `stripe_events/{eventId}`

### User document

Path:
- `users/{userId}`

Recommended shape for the billing-relevant portions:

```ts
type UserDocument = {
  userId: string;

  billing?: {
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

    currentPeriodStart?: string;
    currentPeriodEnd?: string;

    cancelAtPeriodEnd?: boolean;
    canceledAt?: string | null;

    latestInvoiceId?: string;
    latestInvoicePaidAt?: string | null;
    latestInvoicePaymentFailedAt?: string | null;

    isEarlySupporter?: boolean;
    lastCreditRefreshAt?: string | null;
    nextCreditRefreshAt?: string | null;
    creditRefreshAnchor?: string | null;
    freeTierActivatedAt?: string | null;
  };

  credits: {
    balance: number;
    reserved: number;
    expiresAt?: string | null;
    overdrafted: boolean;
    trialGrantedAt?: string | null;
    trial_reset_v1?: boolean;
    monthlyAllowance?: number | null;
    lastGrantType?: string | null;
    lastGrantAt?: string | null;
  };

  createdAt: string;
  updatedAt: string;
};
```

Implementation notes:
- this matches the current code path in `src/backend/credits.py`, which already reads and writes `users/{userId}.credits`
- v1 billing should extend that same user document rather than introducing a separate `users/{userId}/credits/state` subdocument model
- the synthesis backend already depends on the existing `users/{userId}.credits.balance`, `reserved`, `expiresAt`, and `overdrafted` fields
- `billing.activePlanKey` is the source of truth for entitlement
- `credits.monthlyAllowance` is cached operational state only
- `credits.expiresAt` is legacy / unused for recurring free-plan and paid-plan balances going forward
- legacy fields like `trialGrantedAt` and `trial_reset_v1` may remain for compatibility, but new recurring entitlement logic must not depend on them

### Credits ledger entry

Path:
- `credit_ledger/{entryId}`

Suggested shape:

```ts
type CreditLedgerEntry = {
  type:
    | "trial_grant"
    | "trial_reset"
    | "reserve"
    | "settle"
    | "release"
    | "grant_free_monthly"
    | "grant_paid_subscription_cycle"
    | "grant_paid_annual_monthly_refresh"
    | "manual_adjustment"
    | "refund_adjustment";

  userId: string;
  sessionId?: string;
  jobId?: string;
  amount: number; // positive for grants, negative for settled usage, 0 for reserve/release
  reservedDelta?: number;
  reservedAfter?: number;
  balanceAfter: number;

  planKey?: PlanKey;
  stripeInvoiceId?: string;
  stripeSubscriptionId?: string;
  stripeEventId?: string;

  notes?: string | null;
  createdAt: string;
};
```

Implementation notes:
- this spec reuses the existing top-level `credit_ledger` collection instead of introducing a nested `users/{userId}/credits/ledger` collection
- the current implementation already writes:
  - `reserve`
  - `settle`
  - `release`
  - `trial_reset`
- paid billing should add new positive-grant entry types but keep the same ledger shape so credit audit and reconciliation remain in one place
- for monthly paid plans, exactly one grant ledger entry should exist per `stripeInvoiceId`

### Webhook event audit record

Path:
- `stripe_events/{stripeEventId}`

Suggested shape:

```ts
type BillingEventAudit = {
  stripeEventId: string;
  type: string;
  processed: boolean;
  processedAt?: string | null;
  relatedStripeCustomerId?: string;
  relatedStripeSubscriptionId?: string;
  relatedStripeInvoiceId?: string;
  relatedStripeCheckoutSessionId?: string;
  userId?: string | null;
  payloadSummary?: Record<string, unknown>;
  createdAt: string;
};
```

Implementation notes:
- store summary + processing metadata by default
- do not store full raw Stripe event payload unless there is a specific future need

---

## Stripe Customer Mapping

Each Firebase user should map to exactly one Stripe customer.

Recommended fields:
- store `stripeCustomerId` on `users/{userId}.billing`
- also add metadata on the Stripe customer:
  - `firebaseUserId`
  - `environment` (`test` or `live`)

### Rule
On checkout creation:
- if the user already has `stripeCustomerId`, reuse it
- otherwise create a Stripe customer first and persist it

---

## Backend Endpoints

### Shared helper modules
The implementation should define shared helper modules for:
- plan catalog
- Stripe client initialization
- billing-state persistence
- grant/idempotency helpers
- refresh-date calculation
- webhook event processing helpers

This avoids duplication across separate HTTP functions.

### 1. Create Checkout Session

**Function**
- `createCheckoutSession`

**Auth**
- required

**Request**
```json
{
  "planKey": "solo_monthly"
}
```

**Validation rules**
- `planKey` must be a valid selectable paid plan
- `free` is not valid here
- if early supporter is disabled, reject early supporter plan keys
- do not allow multiple prices in one session
- if user already has an active paid subscription, reject checkout and direct them to Manage Billing or support
- use Firestore as the fast-path source of truth for active subscription state
- only verify with Stripe if local state is ambiguous or suspected stale

**Behavior**
1. authenticate user
2. load or create Stripe customer
3. map `planKey` to exactly one Stripe price ID
4. create Stripe Checkout Session in `subscription` mode
5. pass:
   - `customer`
   - `line_items` with one price
   - `success_url`
   - `cancel_url`
6. return session URL

**Response**
```json
{
  "url": "https://checkout.stripe.com/..."
}
```

### 2. Create Customer Portal Session

**Function**
- `createPortalSession`

**Auth**
- required

**Request**
```json
{}
```

**Behavior**
1. authenticate user
2. load `stripeCustomerId`
3. create Stripe portal session with return URL
4. return portal URL

**Response**
```json
{
  "url": "https://billing.stripe.com/..."
}
```

### 3. Stripe Webhook Endpoint

**Function**
- `stripeWebhook`

**Auth**
- Stripe signature verification only

**Requirements**
- read raw request body
- verify Stripe signature using webhook secret
- idempotent processing using `event.id`

### 4. Scheduled Refresh Job

**Function**
- `refreshCredits`

**Schedule**
- once daily

**Responsibility**
- query by `billing.nextCreditRefreshAt <= now`
- apply free-plan refreshes
- apply annual-plan monthly refreshes
- retry due refreshes that were previously skipped because `reserved > 0`
- optionally perform light reconciliation checks

---

## Webhook Events to Handle

At minimum, handle these events:

- `checkout.session.completed`
- `invoice.paid`
- `invoice.payment_failed`
- `customer.subscription.updated`
- `customer.subscription.deleted`

Optionally also:
- `customer.subscription.created`
- `customer.subscription.trial_will_end`
- `invoice.finalized`

### Event handling guidance

#### `checkout.session.completed`
Use to:
- confirm purchase completed
- capture Stripe customer ID
- capture Stripe subscription ID
- associate session with Firebase user
- initialize or update billing state

Do **not** rely only on redirect success page for provisioning.

#### `invoice.paid`
Use to:
- confirm successful billing cycle payment
- update billing state
- refresh credits for **monthly paid plans**
- record paid invoice timestamp
- ensure entitlement remains active

Idempotency rule:
- do not write a monthly paid grant ledger entry if one already exists for the same `stripeInvoiceId`

For annual plans:
- this event confirms initial annual purchase and yearly renewals
- monthly credit refresh still requires the scheduler

#### `invoice.payment_failed`
Use to:
- record failed payment state
- notify user to update payment method
- optionally show billing issue in app

Do not immediately assume cancellation. Follow Stripe subscription status transitions.

#### `customer.subscription.updated`
Use to:
- reflect subscription status changes
- handle `cancel_at_period_end`
- update current period dates
- update cancellation-related flags

#### `customer.subscription.deleted`
Use to:
- mark paid subscription ended
- revert active plan to `free`
- stop future paid refresh logic
- preserve audit history

---

## Entitlement Logic

### Active paid entitlement
A user is considered paid when:
- there is a subscription
- status is one of:
  - `active`

For v1:
- Stripe-managed subscription trials are not used
- the permanent free tier is outside Stripe and must not be represented as Stripe `trialing`

### Free fallback
A user is on free plan when:
- no active paid subscription exists
- or a paid subscription has ended

### Plan source of truth
Use Stripe subscription price ID to map back to internal `planKey`.

Recommended helper:

```ts
function getPlanKeyFromStripePriceId(priceId: string): PlanKey | null
```

---

## Credit Grant Rules

### Current baseline
The current backend credit system already has a usable reservation/settlement flow:
- reserve estimated credits before work starts
- settle actual credits after synthesis completes
- release reservation if the job fails or is canceled

For v1 billing:
- preserve this reservation/settlement flow
- replace the legacy one-time trial grant with the permanent free plan grant model
- implement free-plan monthly refresh as the new default grant path for non-paying users

### Refresh anchor model
A user has one active `creditRefreshAnchor` field representing the current monthly credit cadence.

The anchor determines **when** monthly refreshes occur.
The user’s effective entitlement determines **which monthly allowance** is granted at that refresh.

Rule:
- anchor decides **when**
- entitlement decides **how much**

Anchor transition rules:
- new permanent free users: anchor = first permanent free-tier bootstrap / grant date
- entering paid from free: anchor = first successful paid grant date
- returning to free after paid ends: keep the current anchor
- entering paid again later from free: re-anchor again to that new first successful paid grant date

For annual subscriptions canceled at period end:
- continue granting the annual plan’s monthly allowance on each anchor date until the subscription actually expires
- do not switch to free-tier refresh while the annual paid subscription remains active
- after the paid subscription ends, subsequent free-tier refreshes continue using the current anchor

### Free users
- free users receive 8 credits monthly
- grant happens on a scheduler
- there is no more one-time 20-credit trial
- there is no 30-day trial expiry model for the free tier

Implementation requirements:
- remove or bypass the legacy one-time trial grant path in `get_or_create_credits`
- free users should be initialized into the permanent free plan state
- monthly free refresh should use the same ledger + reservation/settlement architecture as paid plans

### Free-plan refresh cadence
Free-plan credits refresh monthly from the user’s refresh anchor date.

- New permanent free users: anchor = first permanent free-tier bootstrap / grant date.
- For legacy users whose trial is still active at launch: preserve current cadence from the original registration / trial date.
- For legacy users whose trial is already expired at launch: on first re-login after launch, immediately convert to free tier, immediately grant 8 credits, and set the free-tier anchor to that conversion date.
- Future refreshes occur on the same day-of-month as the anchor.
- If that day does not exist in a later month, refresh on the last day of that month.

This is intentionally calendar-month anchored, not a rolling 30-day interval.

### Monthly paid subscribers
- monthly refresh amount is based on plan
- refresh is granted on successful billing cycle confirmation
- refresh is triggered by `invoice.paid`
- if `reserved == 0`, apply the paid reset immediately
- if `reserved > 0`, defer both the paid reset and any paid-entry re-anchor until the reset is actually applied

Normal happy-path examples:
- free -> monthly:
  - first successful `invoice.paid` grants credits immediately
  - that grant date becomes the new `creditRefreshAnchor`
- paid -> free:
  - keep the current anchor
- free again -> monthly later:
  - first successful `invoice.paid` on the new paid start becomes the new `creditRefreshAnchor`

### Annual paid subscribers
- billed once per year
- still receive monthly credits
- first successful `invoice.paid` establishes the paid anchor
- monthly refresh is granted by the daily scheduler using that paid anchor
- if the anchor day does not exist in a later month, refresh on the last day of that month

Normal happy-path example:
- free -> annual:
  - first successful `invoice.paid` grants credits immediately
  - that grant date becomes the new `creditRefreshAnchor`
  - later annual-plan monthly credits are scheduler-driven from that paid anchor date

### Recommended policy
Each refresh/grant updates:
- `users/{userId}.credits.balance`
- `users/{userId}.credits.monthlyAllowance`
- `users/{userId}.billing.lastCreditRefreshAt`
- `users/{userId}.billing.nextCreditRefreshAt`
- and writes a positive grant record to `credit_ledger`

Preferred simple model for v1:
- refresh **resets balance to the monthly allowance**
- unused credits do not roll over

Implementation note:
- because the current system already tracks `reserved` credits separately, the refresh job must not mutate balances while credits are actively reserved
- do not delete or rewrite existing reservation documents during monthly refresh

### Deferred refresh without a pending flag
A daily scheduled job determines whether a user’s monthly credit refresh is due based on:
- `billing.nextCreditRefreshAt`
- `billing.lastCreditRefreshAt`
- active anchor-derived initialization/repair logic

If refresh is due:
- and `reserved == 0`, apply the refresh
- and `reserved > 0`, skip the user for now and retry on the next scheduled run

This avoids mutating balances during in-flight generation without requiring a separate `refreshPending` flag.

### Legacy non-paying user migration
At launch of Stripe paid plans, the existing user base contains legacy trial users only. There are no previously launched paid-plan subscribers to migrate from an earlier Stripe paid system.

Migration therefore applies to two launch cohorts:

- legacy users whose trial is still active at launch
- legacy users whose trial is already expired at launch

For both cohorts:
- preserve existing balance until the next due free refresh
- do not immediately clamp legacy balances down to 8 credits during migration
- if a legacy user is overdrafted, leave that state as-is until the next due free refresh

Anchor initialization during migration:
- still-active legacy trial users: initialize `billing.creditRefreshAnchor` from the original registration / trial date cadence
- expired legacy trial users: set `billing.creditRefreshAnchor` only when they first re-login and are converted to permanent free tier
- later free -> paid entry re-anchors on the first successful paid grant date

At the next scheduled free-plan refresh:
- reset balance to 8 credits
- leave `reserved` unchanged
- continue using normal permanent free-plan monthly refresh rules after that

Additional rule for expired legacy trial users:
- on first re-login after launch, the expired trial is upgraded to permanent free tier immediately
- immediately grant 8 credits
- set free-tier anchor to that conversion date
- record a one-time marker such as `billing.freeTierActivatedAt` so this conversion cannot retrigger later

---

## Credit Refresh Scheduler

A scheduled job is required.

### Responsibility
Run once daily and:
- query users whose `billing.nextCreditRefreshAt <= now`
- check current billing state
- determine eligible allowance from current effective entitlement
- if `reserved == 0`, reset `users/{userId}.credits.balance` to the correct allowance
- leave `users/{userId}.credits.reserved` untouched
- write a positive ledger entry to `credit_ledger`
- move `billing.lastCreditRefreshAt`
- compute new `billing.nextCreditRefreshAt`

If refresh is due but `reserved > 0`:
- skip the user for that run
- retry on the next daily run

### Cases
1. **Free**
   - grant 8 credits monthly

2. **Solo Monthly**
   - refreshed by `invoice.paid`
   - scheduler may serve as repair/reconciliation only if needed

3. **Solo Annual**
   - refresh monthly via scheduler

4. **Choir Monthly**
   - refreshed by `invoice.paid`

5. **Choir Annual / Choir Early Annual**
   - refresh monthly via scheduler

### Suggested simplification
For v1:
- use `invoice.paid` as the grant trigger for monthly paid plans
- use the scheduler for free-plan refreshes and annual-plan monthly refreshes
- allow the scheduler to act as a repair path if monthly webhook-driven refresh state ever needs reconciliation

---

## UI Requirements

### Pricing page
Show:
- Free
- Solo monthly
- Solo annual
- either early supporter Choir prices or standard Choir prices depending on config flag

### Billing/account page
Show:
- current plan
- remaining credits
- monthly credit allowance
- next credit refresh date
- billing status
- cancellation status if applicable
- button: **Manage billing**
- button: **Upgrade** or **Choose plan** if on free

If the user already has an active paid subscription:
- do not show another paid checkout path
- instead show **Manage Billing**
- optionally show messaging such as “Plan changes coming soon” or direct users to support

### Success and cancel pages
Success page:
- should confirm purchase
- should not itself grant access
- may poll backend for updated billing state
- should provide a clear path back to `/app/settings/billing`

Cancel page:
- should show non-error message
- user can retry checkout

---

## Analytics and Logging

Track these events internally:
- pricing page viewed
- checkout started
- checkout completed
- portal opened
- cancellation requested
- subscription canceled
- invoice paid
- invoice payment failed
- credits refreshed
- credits consumed

Recommended joined analytics dimensions:
- planKey
- billing interval
- early supporter vs standard
- signup cohort
- first successful generation before conversion
- churn by usage level

---

## Security and Reliability

### Stripe secrets
Store in Secret Manager / environment config only.

### Webhook verification
Mandatory:
- verify Stripe signature
- reject unsigned/invalid payloads

### Idempotency
Mandatory:
- store processed Stripe event IDs
- no event should grant credits twice

### Access control
- all billing endpoints require authenticated user except webhook
- checkout session creation must never trust client-supplied Stripe price ID directly
- client sends only internal `planKey`

### Auditability
Keep:
- Stripe event audit log
- credit ledger
- billing state snapshots

---

## Testing Checklist

### Catalog/config
- all Stripe price IDs are correct
- early supporter toggle works

### Checkout
- user can start checkout for each selectable plan
- checkout uses exactly one price
- user with active paid subscription cannot start a second paid checkout
- cancel returns to cancel page
- success returns to success page

### Webhooks
- `checkout.session.completed` processed once
- `invoice.paid` processed once
- `invoice.payment_failed` handled cleanly
- `customer.subscription.updated` updates cancellation flags
- `customer.subscription.deleted` reverts to free

### Portal
- open portal successfully
- update payment method successfully
- view invoice history
- cancel at end of billing period
- cancellation reason collected
- return URL goes back to `/app/settings/billing`

### Credits
- new paid user gets correct allowance
- free-plan monthly refresh works from anchor date
- annual subscriber monthly refresh works from anchor date
- monthly paid refresh works from `invoice.paid`
- refresh never duplicates
- refresh is skipped while `reserved > 0` and applied on a later daily run

### Legacy migration
- existing non-paying users are normalized into the permanent free-tier logic without forcing immediate clamp
- previously-paid-but-currently-free users preserve their existing anchor if present; otherwise anchor from original account creation time
- still-active trial users preserve current cadence until natural boundary
- expired trial users convert on first re-login, receive 8 credits, and get a new free-tier anchor based on that conversion date
- account creation / original registration timestamp is preserved where applicable for cadence
- legacy trial fields no longer drive entitlement logic

### Reconciliation
- app billing state matches Stripe state
- missing webhook can be repaired manually/admin-side if needed

---

## Recommended Implementation Order

1. add plan mapping config
2. implement shared billing helper modules
3. implement Stripe customer lookup/create
4. implement checkout session endpoint
5. implement portal session endpoint
6. implement webhook endpoint with signature verification
7. implement billing state persistence
8. implement credit ledger and credits state extensions
9. implement refresh scheduler
10. implement legacy user migration logic
11. implement pricing page integration
12. implement account/billing page
13. run end-to-end tests in Stripe test mode
14. launch soft rollout

---

## Open Questions

These should be decided explicitly before production launch:

1. How should support/admin manually correct billing or credits?

No other major product-rule ambiguities remain for v1.

---

## Recommended v1 Defaults

- no self-serve plan switching
- no custom upgrade flow in initial launch
- no parallel or stacked subscriptions
- cancellation at end of billing period
- no rollover credits
- early supporter pricing controlled by environment flag
- Stripe portal handles invoices, cancellation, payment method updates
- SightSinger handles entitlements and credits
- annual subscriptions receive monthly credit refresh through app scheduler
- monthly paid subscriptions refresh via `invoice.paid`
- the existing reservation / settle / release credit flow remains unchanged
- the permanent free tier is 8 credits/month and replaces the legacy one-time 20-credit trial
- free-plan refresh is monthly from refresh anchor
- if `reserved > 0`, skip refresh and retry on a later daily scheduler run
- Firestore is the fast-path source of truth; Stripe is used when local state is ambiguous or stale

---

## Summary

This v1 billing design keeps the billing surface area small and reliable:

- Stripe owns payment collection and subscription administration
- SightSinger owns credits, entitlements, and analytics
- billing runs on Cloud Functions for Firebase (2nd gen)
- Checkout is created server-side with one Stripe price per session
- Customer Portal provides low-maintenance self-service
- Webhooks plus scheduler keep billing and monthly credits in sync
