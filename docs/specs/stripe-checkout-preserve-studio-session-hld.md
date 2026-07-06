# Stripe Checkout Preserve Studio Session HLD

## Status

Draft

## Problem

The current Stripe checkout flow redirects the main Studio tab to Stripe-hosted Checkout and then returns to `/app?...`. That return is a full page load, so the in-memory Studio state is lost:

- uploaded or derived score preview state
- generated audio blobs and multitrack player lanes
- pending user intent after an insufficient-credit paywall
- any work the user has not downloaded or exported yet

This is especially bad when the paywall appears because the user is blocked by low credits. After buying credits or upgrading, the expected UX is to continue the same synthesis/export action in the same Studio session.

## Key Constraint

With Stripe-hosted Checkout opened in the same browser tab, the app necessarily navigates away. The webhook can update Firestore asynchronously, but it cannot keep the original JavaScript runtime alive after the tab has left the app.

To avoid losing Studio state, the checkout UI must either:

1. run without navigating the main app tab, or
2. persist and rehydrate enough Studio state after return.

## Recommendation

Use **Stripe Embedded Checkout inside the existing paywall modal** for both:

- subscription upgrade checkout
- one-time top-up credit checkout

For card and supported non-redirect flows that complete inside Embedded Checkout, the Studio app remains mounted, so generated score/audio state stays in memory. After checkout completes, the paywall waits for backend billing confirmation, then closes automatically.

For the initial implementation, disable redirect-based payment methods. The product decision is that preserving the Studio session is more important than supporting payment methods that require navigating to a bank app or external approval page.

## Goals

- Do not refresh or unload the Studio page for normal checkout completion.
- Keep existing webhook-driven billing as the source of truth.
- Close the paywall only after the user entitlement or credit balance has actually updated.
- Let the user continue the blocked workflow after purchase.
- Support both subscription upgrades and top-up credit purchases.
- Preserve current Stripe Checkout Sessions integration where practical.
- Disable redirect-based payment methods in this phase so checkout cannot unload the Studio page.

## Non-Goals

- Replacing Stripe Checkout with a custom Payment Element flow.
- Changing credit pricing or subscription plan behavior.
- Making checkout work offline.
- Building a complete Studio session restore system in this phase.
- Auto-resubmitting synthesis/export actions after purchase. This can be a later UX improvement.
- Persisting and restoring a full Studio session after external payment redirects.

## UX Flow

### Top-Up Credits

1. User attempts a billable action with insufficient credits.
2. Paywall opens over the existing Studio state.
3. User selects credit pack.
4. App creates an embedded top-up Checkout Session.
5. Paywall replaces pricing cards with the embedded Stripe checkout frame.
6. User pays inside the modal.
7. Checkout completion switches modal to `Confirming payment...`.
8. Backend webhook grants the top-up pack and updates `users/{uid}.topupCredits`.
9. Existing Firestore listeners receive the new credit balance.
10. Paywall closes automatically.
11. User continues the same Studio session.

### Subscription Upgrade

1. User selects Solo or Pro in the paywall.
2. App creates an embedded subscription Checkout Session.
3. User completes checkout in the modal.
4. Checkout completion switches modal to `Activating plan...`.
5. Webhook applies subscription entitlement and monthly credits.
6. Firestore listener receives updated billing state.
7. Paywall closes automatically.

## Architecture

```text
Studio Paywall
  |
  | POST /billing/embedded-checkout-session
  v
Billing Backend
  |
  | create Stripe Checkout Session
  | ui_mode=embedded_page
  | mode=payment or subscription
  v
Stripe Embedded Checkout
  |
  | user completes payment
  v
Stripe Webhook
  |
  | transactional billing update
  v
Firestore users/{uid}, topup_packs, credit_ledger
  |
  | existing listeners
  v
Studio Paywall closes without page reload
```

## Backend Design

### New Embedded Checkout Endpoint

Add an endpoint that creates a Checkout Session for embedded UI and returns the client secret:

```http
POST /billing/embedded-checkout-session
```

Request:

```json
{
  "checkoutType": "subscription",
  "planKey": "solo_monthly"
}
```

or:

```json
{
  "checkoutType": "topup",
  "packKey": "topup_15"
}
```

Response:

```json
{
  "checkoutSessionId": "cs_test_...",
  "clientSecret": "cs_test_..._secret_...",
  "checkoutType": "topup"
}
```

### Stripe Session Creation

Use the same product, price, metadata, customer, and top-up hold logic as the existing redirect Checkout flow, but create the session with embedded UI settings.

Common fields:

- `ui_mode = "embedded_page"`
- `mode = "payment"` for top-up
- `mode = "subscription"` for plans
- `client_reference_id = uid`
- metadata:
  - `firebaseUserId`
  - `purchaseType = "topup"` for top-up
  - `planKey` for subscription
  - existing top-up hold metadata for pack limit enforcement

For embedded sessions:

- do not use `success_url`
- do not use `cancel_url`
- set `redirect_on_completion = "never"`
- do not enable redirect-based payment methods

This guarantees the normal checkout path cannot navigate away from the Studio page.

### Webhook Handling

Keep webhook processing as the authoritative state transition:

- `checkout.session.completed` with `purchaseType=topup` grants top-up packs.
- subscription checkout events apply the paid entitlement and monthly credits.
- all credit grants and entitlement changes keep existing ledger/audit records.

The client should never grant credits just because the embedded checkout says payment is complete.

### Client-Initiated Reconciliation

Webhook delivery can lag. Add explicit sync endpoints for faster confirmation:

```http
POST /billing/checkout-session/sync
POST /billing/topup-checkout-session/sync
```

The existing subscription sync endpoint can be reused if it already verifies session ownership and Stripe state. Top-up needs the same pattern:

- verify Firebase user
- retrieve Checkout Session from Stripe
- verify `client_reference_id`, `customer`, and metadata belong to the user
- if paid/completed, apply the same idempotent top-up grant path as the webhook
- return current billing/credit summary

This is a convenience accelerator, not the source of truth. Webhook idempotency must still handle the same event later.

## Frontend Design

### Paywall State Machine

Extend `BillingPaywallModal` with these states:

```ts
type CheckoutViewState =
  | "plans"
  | "creating_checkout"
  | "embedded_checkout"
  | "confirming"
  | "complete"
  | "failed";
```

Suggested behavior:

- `plans`: current pricing/top-up choices.
- `creating_checkout`: disable buttons and call backend.
- `embedded_checkout`: mount Stripe Embedded Checkout.
- `confirming`: checkout completed; wait for Firestore billing/credit state or sync endpoint.
- `complete`: close modal.
- `failed`: show recoverable error and let user return to plan choices.

### Embedded Checkout Mount

Load Stripe.js once and mount Embedded Checkout inside the paywall body.

The parent app stays on `/app`; no route navigation occurs for normal card checkout.

### Completion Detection

After embedded checkout completion:

1. Store the expected checkout context in React state:
   - checkout type
   - session id
   - previous available credits
   - previous active plan
2. Switch modal to confirming.
3. Start a short reconciliation loop:
   - call sync endpoint once immediately
   - rely on Firestore listener updates
   - optionally retry sync with backoff for 30-60 seconds
4. Close paywall when:
   - top-up: available credits increased or the expected pack appears
   - subscription: `activePlanKey` changes from free to selected plan, or subscription status becomes active/trialing

If confirmation times out, keep the modal open with a message like:

> Payment received. We are still confirming your credits. You can keep this window open or retry sync.

### Preserve Blocked Intent

When the paywall opens from a blocked action, store a lightweight pending intent in memory:

```ts
type PendingBillingIntent =
  | { type: "synthesize"; partId: string; verseNumber?: string }
  | { type: "export_mix" }
  | { type: "manual_billing_menu" };
```

In this phase, use it only for post-checkout messaging and button focus. Do not auto-run the action until we add explicit confirmation UX.

Example after close:

- show a banner: `Credits added. You can continue rendering.`
- keep the original score, multitrack lanes, and selected controls unchanged.

## Redirect-Based Payment Methods

Redirect-based payment methods are disabled for this phase.

Rationale:

- The current problem is caused by losing in-memory Studio state on navigation.
- Stripe supports `redirect_on_completion = "never"` for embedded Checkout, which disables redirect-based payment methods.
- Without a full Studio session restore project, enabling redirect-heavy payment methods would reintroduce the same class of UX failure.

Out of scope for this phase:

- `return_url` recovery
- `sessionStorage` checkout recovery
- full Studio score/audio state persistence
- external bank-app approval redirects

## Data Model Changes

No required Firestore schema change for the main embedded flow.

Optional diagnostics fields on checkout/session records:

- `checkoutUiMode`: `"hosted"` or `"embedded"`
- `checkoutStartedFrom`: paywall trigger
- `checkoutCompletedAt`
- `clientConfirmedAt`

These are useful for debugging conversion and confirmation timing.

## Security

- Backend must create all Checkout Sessions.
- Frontend receives only `clientSecret` and `checkoutSessionId`.
- Frontend must not pass arbitrary prices, quantities, or credit amounts.
- Backend must validate user identity from Firebase auth.
- Sync endpoints must verify Stripe session ownership before applying changes.
- Top-up pack limit enforcement remains server-side.
- Webhook processing remains idempotent and authoritative.

## Analytics

Add or keep GA events:

- `billing_paywall_open`
- `billing_checkout_start`
- `billing_checkout_embedded_mount`
- `billing_checkout_complete_client`
- `billing_checkout_confirmed`
- `billing_checkout_timeout`
- `billing_checkout_failed`

Recommended event dimensions:

- `checkout_type`: `subscription` or `topup`
- `plan_key` or `pack_key`
- `trigger`: insufficient credits, billing menu, export mix, synthesis
- `ui_mode`: `embedded`

## Logging

Backend logs:

- embedded checkout session created
- checkout type and safe product key
- Stripe session id
- webhook applied
- sync endpoint applied/no-op
- confirmation latency if available

Avoid logging:

- card/payment details
- full client secret
- raw webhook payloads outside existing secure debug handling

## Rollout Plan

### Phase 1: Embedded Top-Up Checkout

- Implement embedded checkout endpoint for top-up packs.
- Mount Embedded Checkout in paywall.
- Add top-up sync endpoint.
- Close paywall after credit listener sees updated balance.

This addresses the highest-friction case: user runs out of credits mid-work and buys a pack.

### Phase 2: Embedded Subscription Checkout

- Add embedded subscription checkout path.
- Reuse modal checkout state machine.
- Close after billing listener sees active/trialing subscription and refreshed credits.

### Phase 3: Recovery Improvements

- Deferred. Do not implement in the initial embedded checkout project.
- A future project may add persisted Studio session restore before enabling redirect-based payment methods.

## Test Plan

### Unit Tests

- Backend creates embedded top-up session with correct metadata and adjustable quantity cap.
- Backend creates embedded subscription session with correct plan metadata.
- Top-up sync endpoint rejects sessions belonging to another user.
- Top-up sync endpoint is idempotent with webhook already applied.
- Subscription sync remains idempotent.

### Frontend Tests

- Paywall transitions from pricing to embedded checkout.
- Closing is blocked while confirmation is pending.
- Paywall closes when credit balance increases.
- Paywall closes when active plan updates.
- Timeout state keeps Studio page mounted.

### Manual QA

- Upload score, synthesize one part, trigger low-credit paywall, buy top-up, verify:
  - no full page refresh
  - score preview remains
  - multitrack lanes remain
  - new credits appear
  - paywall closes
  - user can continue synthesis/export
- Repeat for subscription upgrade.
- Test webhook delay by temporarily stopping the Stripe listener:
  - checkout completes
  - modal stays in confirming state
  - after listener resumes, modal closes
- Test cancel/back from embedded checkout:
  - modal returns to pricing cards
  - Studio state remains unchanged
- Confirm redirect-based payment methods are not offered in the embedded checkout.

## Product Decisions

- Redirect-based payment methods are disabled initially to guarantee no checkout navigation.
- After successful checkout, the paywall closes and the user retries the blocked action manually. The app does not auto-resume synthesis/export.
- Do not build persisted Studio session restore before this phase.
