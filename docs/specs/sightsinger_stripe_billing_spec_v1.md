# SightSinger Stripe Billing Integration Spec (v1)

## Overview

This document defines the v1 paid subscription implementation for SightSinger using **Stripe Checkout + Stripe Customer Portal + webhooks**.

Goals:

- Launch paid plans with minimal billing complexity
- Keep credits and entitlements managed inside SightSinger
- Use Stripe for payment collection, receipts, invoices, and subscription self-service
- Avoid self-serve plan switching in v1
- Support annual billing while still refreshing credits monthly

Out of scope for v1:

- Usage-based billing in Stripe
- Self-serve plan upgrades/downgrades
- Coupon campaigns except Stripe portal retention coupons
- Multi-currency pricing
- Tax ID / VAT handling beyond basic billing details
- Enterprise/custom invoicing flows

---

## Final Pricing Model

### Free
- 8 credits per month
- 1 credit = approximately 30 seconds of audio

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

Future versions may add plan switching later.

### Cancellation
- Cancellation is **at end of billing period**
- User keeps paid access until current paid period ends
- User keeps credits already granted for the active billing cycle unless manually adjusted by admin later

### Failed payments
v1 behavior:
- Stripe remains source of truth for payment state
- SightSinger listens for `invoice.payment_failed`
- User should be notified to update payment method
- Paid entitlement can remain until Stripe subscription status changes to a non-active/non-trialing state, depending on webhook state handling

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

---

## Architecture

### Billing model
Stripe handles:
- subscription billing
- hosted Checkout
- hosted Customer Portal
- invoices
- receipts
- payment methods
- cancellation requests

SightSinger handles:
- user authentication
- customer-to-user mapping
- plan entitlements
- credits
- monthly refresh logic
- product analytics joined with billing data

### Core integration pattern
1. User signs in to SightSinger
2. User selects a paid plan
3. Frontend sends selected `planKey` to backend
4. Backend creates Stripe Checkout Session for exactly one Stripe Price ID
5. User completes payment on Stripe Checkout
6. Stripe sends webhook events
7. SightSinger persists billing state to Firestore
8. SightSinger grants entitlement and credits
9. User can later open Stripe Customer Portal from SightSinger account page

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

---

## Firestore Data Model

Recommended collections:

- `users/{userId}`
- `users/{userId}/billing/state`
- `users/{userId}/billing/events/{eventId}`
- `users/{userId}/credits/state`
- `users/{userId}/credits/ledger/{entryId}`

### Billing state document

Path:
- `users/{userId}/billing/state`

Suggested shape:

```ts
type BillingState = {
  userId: string;

  stripeCustomerId?: string;
  stripeSubscriptionId?: string;
  stripeCheckoutSessionId?: string;

  activePlanKey: PlanKey;
  subscriptionStatus:
    | "none"
    | "trialing"
    | "active"
    | "past_due"
    | "canceled"
    | "incomplete"
    | "incomplete_expired"
    | "unpaid";

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

  createdAt: string;
  updatedAt: string;
};
```

### Credits state document

Path:
- `users/{userId}/credits/state`

Suggested shape:

```ts
type CreditsState = {
  userId: string;

  activePlanKey: PlanKey;
  monthlyCreditAllowance: number;
  remainingCredits: number;

  lastCreditRefreshAt?: string | null;
  nextCreditRefreshAt?: string | null;
  creditRefreshAnchor?: string | null;

  createdAt: string;
  updatedAt: string;
};
```

### Credits ledger entry

Path:
- `users/{userId}/credits/ledger/{entryId}`

Suggested shape:

```ts
type CreditLedgerEntry = {
  type:
    | "grant_free_monthly"
    | "grant_paid_monthly"
    | "grant_paid_annual_monthly_refresh"
    | "usage_audio_generation"
    | "manual_adjustment"
    | "refund_adjustment";

  amount: number; // positive for grants, negative for usage
  resultingBalance: number;

  planKey?: PlanKey;
  stripeInvoiceId?: string;
  stripeSubscriptionId?: string;
  stripeEventId?: string;

  notes?: string | null;
  createdAt: string;
};
```

### Webhook event audit record

Path:
- `users/{userId}/billing/events/{stripeEventId}`

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
  userId?: string | null;
  payloadSummary?: Record<string, unknown>;
  createdAt: string;
};
```

---

## Stripe Customer Mapping

Each Firebase user should map to exactly one Stripe customer.

Recommended fields:
- store `stripeCustomerId` on the billing state document
- also add metadata on the Stripe customer:
  - `firebaseUserId`
  - `environment` (`test` or `live`)

### Rule
On checkout creation:
- if the user already has `stripeCustomerId`, reuse it
- otherwise create a Stripe customer first and persist it

---

## Backend Endpoints

### 1. Create Checkout Session

**Endpoint**
- `POST /api/billing/create-checkout-session`

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
- do not allow checkout if there is already an incompatible active subscription unless explicitly handled

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

**Endpoint**
- `POST /api/billing/create-portal-session`

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

**Endpoint**
- `POST /api/billing/webhook`

**Auth**
- Stripe signature verification only

**Requirements**
- read raw request body
- verify Stripe signature using webhook secret
- idempotent processing using `event.id`

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
- refresh credits for monthly plans
- record paid invoice timestamp
- ensure entitlement remains active

For annual plans:
- this event confirms initial annual purchase and yearly renewals
- monthly credit refresh still requires a scheduler

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
  - `trialing` (not currently planned, but safe to support if introduced later)

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

### Free users
- 8 credits granted monthly
- grant happens on a scheduler based on the app’s free-plan refresh policy
- if you prefer calendar-month semantics, implement consistently and document clearly

### Monthly paid subscribers
- monthly refresh amount based on plan
- grant on successful billing cycle confirmation
- usually triggered by `invoice.paid`

### Annual paid subscribers
- billed once per year
- still receive monthly credits
- monthly refresh must be granted by SightSinger scheduler, not only Stripe renewals

### Recommended policy
Each refresh sets:
- `monthlyCreditAllowance`
- `remainingCredits`

Preferred simple model for v1:
- refresh **resets remaining credits to the monthly allowance**
- unused credits do not roll over

If you want rollover later, that is a separate product rule and should not be mixed into v1.

---

## Credit Refresh Scheduler

A scheduled job is required.

### Responsibility
Run at least daily and:
- find users whose `nextCreditRefreshAt <= now`
- check current billing state
- determine eligible allowance
- refresh credits
- write ledger entry
- move `lastCreditRefreshAt`
- compute new `nextCreditRefreshAt`

### Cases
1. **Free**
   - grant 8 credits monthly

2. **Solo Monthly**
   - normally refreshed by `invoice.paid`
   - scheduler may also serve as repair/reconciliation if desired

3. **Solo Annual**
   - refresh monthly via scheduler

4. **Choir Monthly**
   - normally refreshed by `invoice.paid`

5. **Choir Annual / Choir Early Annual**
   - refresh monthly via scheduler

### Suggested simplification
For v1, you may let the scheduler handle **all** monthly refreshes, as long as active entitlement is checked against billing state.  
That can reduce branching and keep behavior uniform.

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

### Success and cancel pages
Success page:
- should confirm purchase
- should not itself grant access
- may poll backend for updated billing state

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

### Credits
- new paid user gets correct allowance
- monthly refresh works
- annual subscriber monthly refresh works
- refresh never duplicates
- free plan refresh works

### Reconciliation
- app billing state matches Stripe state
- missing webhook can be repaired manually/admin-side if needed

---

## Recommended Implementation Order

1. add plan mapping config
2. implement Stripe customer lookup/create
3. implement checkout session endpoint
4. implement portal session endpoint
5. implement webhook endpoint with signature verification
6. implement billing state persistence
7. implement credit ledger and credits state
8. implement refresh scheduler
9. implement pricing page integration
10. implement account/billing page
11. run end-to-end tests in Stripe test mode
12. launch soft rollout

---

## Open Questions

These should be decided explicitly before production launch:

1. What exact URL will be used for portal return?
2. What exact free-plan refresh cadence should be used?
   - rolling every 30 days from account creation
   - monthly calendar boundary
   - monthly from first refresh anchor
3. Should paid unused credits expire on each refresh?
   - recommended v1: yes, reset monthly, no rollover
4. How should support/admin manually correct billing or credits?
5. What is the exact date or rule for disabling early supporter plans?

---

## Recommended v1 Defaults

- no self-serve plan switching
- cancellation at end of billing period
- no rollover credits
- early supporter pricing controlled by config flag
- Stripe portal handles invoices, cancellation, payment method updates
- SightSinger handles entitlements and credits
- annual subscriptions receive monthly credit refresh through app scheduler

---

## Summary

This v1 billing design keeps the billing surface area small and reliable:

- Stripe owns payment collection and subscription administration
- SightSinger owns credits, entitlements, and analytics
- Checkout is created server-side with one Stripe price per session
- Customer Portal provides low-maintenance self-service
- Webhooks plus scheduler keep billing and monthly credits in sync
