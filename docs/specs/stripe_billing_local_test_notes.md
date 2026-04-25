# Stripe Billing Local Test Notes

## Purpose

This note records the local test setup and the billing behavior that has already been verified in the current implementation slice.

## Local prerequisites

Set the Stripe billing env vars in `env/dev.env`:

- non-secret config is already present
- add secrets locally only:
  - `STRIPE_SECRET_KEY=sk_test_...`
  - `STRIPE_WEBHOOK_SECRET=whsec_...`
  - `STRIPE_PORTAL_CONFIGURATION_ID=bpc_...`

Start required local services:

1. Firestore emulator
2. backend API
3. Stripe webhook forwarding

Example webhook forwarding command:

```bash
stripe listen --forward-to localhost:8000/billing/webhook
```

Use the printed `whsec_...` value for `STRIPE_WEBHOOK_SECRET`.

## Focused test command

Run:

```bash
../ai-singer-diffsinger/.venv/bin/python -m pytest tests/test_billing.py tests/test_credits.py -q
```

Latest local result in this branch:

- `19 passed`

## What has been tested

### Billing unit/integration coverage

Verified in `tests/test_billing.py`:

- monthly refresh date calculation preserves anchor time and handles month-end rollover
- checkout session creation:
  - creates Stripe customer when missing
  - creates Stripe Checkout session
  - stores `billing.stripeCustomerId`
  - stores `billing.stripeCheckoutSessionId`
- portal session creation rejects users without an existing Stripe customer
- `invoice.paid` for monthly paid plans:
  - grants immediately when `credits.reserved == 0`
  - updates balance to paid monthly allowance
  - re-anchors `billing.creditRefreshAnchor` on first paid grant after free
- deferred monthly paid reset:
  - `invoice.paid` updates billing state but does not mutate balance while `reserved > 0`
  - no positive grant ledger row is written until the deferred reset is actually applied
  - scheduler later applies the reset once `reserved == 0`
- annual paid first cycle:
  - first successful `invoice.paid` grants immediately
  - paid anchor is established from the paid start date
  - `billing.nextCreditRefreshAt` is computed from that paid anchor

### Credits and migration coverage

Verified in `tests/test_credits.py`:

- new-user free-tier bootstrap grants 8 credits with no expiry
- still-active legacy trial migration preserves existing balance and original cadence anchor
- expired legacy trial migration converts to permanent free tier and grants 8 credits
- credit estimation logic still matches the 30-second block model
- reservation flow:
  - success path
  - insufficient balance
  - duplicate reservation idempotency
- settlement flow:
  - exact settlement
  - overdraft behavior
  - atomic settle-and-complete job path
  - idempotent retry behavior
- release flow:
  - normal release
  - release after settlement
- reconciliation-required marker updates reservation state correctly

## What has not been tested yet

Not yet covered by live local/manual testing:

- real Stripe Checkout redirect flow in browser
- real Stripe Billing Portal launch in browser
- dashboard- or CLI-triggered live webhook delivery against the running backend
- `checkout.session.completed`
- `invoice.payment_failed`
- `customer.subscription.updated`
- `customer.subscription.deleted`
- end-to-end frontend wiring from the React account page
- deployed Cloud Functions runtime behavior

## Recommended next manual checks

1. create a checkout session from the app/backend and verify redirect URL
2. complete a sandbox subscription purchase and confirm webhook writes in Firestore
3. trigger `invoice.payment_failed` and confirm billing mirror updates
4. cancel in the portal and confirm `customer.subscription.updated` / `deleted` handling
5. verify returning paid users are blocked from starting a second checkout
