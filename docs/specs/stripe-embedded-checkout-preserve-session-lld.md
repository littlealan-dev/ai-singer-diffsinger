# Stripe Embedded Checkout Preserve Studio Session LLD

## Status

Draft

## Parent Design

HLD: [stripe-checkout-preserve-studio-session-hld.md](stripe-checkout-preserve-studio-session-hld.md)

## Purpose

Replace same-tab Stripe-hosted checkout for Studio paywall purchases with Stripe Embedded Checkout rendered inside the paywall modal, so the Studio page does not unload and generated score/audio state remains available after purchase.

This LLD covers:

- one-time top-up credit checkout
- subscription plan checkout
- client-side confirmation and modal close
- backend sync endpoints for webhook lag
- tests and rollout

## Product Decisions

- Disable redirect-based payment methods in this phase.
- Use Stripe Embedded Checkout with `redirect_on_completion = "never"`.
- Do not provide a `return_url` for embedded checkout in this phase.
- Do not auto-resume a blocked synthesis/export after purchase. The user retries manually.
- Do not build full Studio session persistence/restore before this phase.

## Existing Flow Summary

Current checkout starts by calling:

- `POST /billing/checkout-session`
- `POST /billing/topup-checkout-session`

The backend returns a Stripe-hosted Checkout URL. The frontend assigns `window.location.href`, which unloads `/app`. After Stripe returns to `/app?...`, Studio state is gone.

The existing webhook and Firestore listener model is otherwise correct and should remain the billing source of truth.

## New Flow Summary

```text
User clicks top-up/upgrade in paywall
  -> frontend calls /billing/embedded-checkout-session
  -> backend creates Stripe Checkout Session with ui_mode=embedded_page
  -> backend returns { checkoutSessionId, clientSecret }
  -> frontend mounts Embedded Checkout in paywall
  -> user completes payment in iframe
  -> Stripe calls frontend onComplete
  -> frontend enters confirming state
  -> frontend calls sync endpoint and listens to Firestore
  -> webhook/sync applies credit or subscription state
  -> Firestore listener updates billing state
  -> paywall closes, Studio state remains mounted
```

## Backend Changes

### Config

Add publishable key env config for frontend delivery if not already available:

```env
STRIPE_PUBLISHABLE_KEY=pk_test_...
```

No new `return_url`, `success_url`, or `cancel_url` is needed for embedded non-redirect checkout.

Existing top-up config remains:

- `STRIPE_PRICE_TOPUP_15`
- `TOPUP_PACK_EXPIRY_DAYS`
- `TOPUP_PACK_CREDIT_AMOUNT`
- `TOPUP_MAX_ACTIVE_PACKS`
- `TOPUP_CHECKOUT_HOLD_TTL_MINUTES`

Existing subscription price config remains unchanged.

### API: Create Embedded Checkout Session

Add to both billing apps if both expose billing routes:

- `src/backend/main.py`
- `src/backend/billing_api.py`

Endpoint:

```http
POST /billing/embedded-checkout-session
Authorization: Bearer <firebase id token>
Content-Type: application/json
```

Request type:

```ts
type EmbeddedCheckoutRequest =
  | {
      checkoutType: "topup";
      packKey: "topup_15";
    }
  | {
      checkoutType: "subscription";
      planKey: BillingPlanKey;
    };
```

Response type:

```ts
type EmbeddedCheckoutResponse = {
  checkoutSessionId: string;
  clientSecret: string;
  checkoutType: "topup" | "subscription";
};
```

Errors:

- `400`: invalid checkout type, invalid pack, invalid paid plan
- `401`: unauthenticated
- `409`: active paid subscription already exists, or top-up active pack limit reached
- `503`: Stripe/config unavailable

### Service Function

Create a backend helper:

```python
def create_embedded_checkout_session(
    uid: str,
    email: str,
    request: EmbeddedCheckoutRequest,
    *,
    config: BillingConfig | None = None,
    stripe_client: Any | None = None,
) -> EmbeddedCheckoutSessionResult:
    ...
```

Suggested result:

```python
@dataclass(frozen=True)
class EmbeddedCheckoutSessionResult:
    checkout_session_id: str
    client_secret: str
    checkout_type: Literal["topup", "subscription"]
```

Implementation can delegate to lower-level helpers:

- `create_embedded_topup_checkout_session(...)`
- `create_embedded_subscription_checkout_session(...)`

### Stripe Params: Top-Up

Use the same logic as redirect top-up checkout:

- ensure or create Stripe customer
- expire stale active packs/holds
- calculate remaining top-up pack slots
- create pending hold before session creation
- use adjustable quantity maximum equal to remaining slots
- persist Stripe session id on hold

Stripe session params:

```python
params = {
    "ui_mode": "embedded_page",
    "redirect_on_completion": "never",
    "mode": "payment",
    "customer": stripe_customer_id,
    "line_items": [
        {
            "price": config.stripe_price_topup_15,
            "quantity": 1,
            "adjustable_quantity": {
                "enabled": True,
                "minimum": 1,
                "maximum": remaining_slots,
            },
        }
    ],
    "client_reference_id": uid,
    "metadata": {
        "firebaseUserId": uid,
        "purchaseType": "topup",
        "packKey": "topup_15",
        "creditAmount": str(config.topup_pack_credit_amount),
        "checkoutHoldId": hold_id,
        "maxQuantity": str(remaining_slots),
        "checkoutUiMode": "embedded",
    },
}
```

Do not include:

- `success_url`
- `cancel_url`
- `return_url`

### Stripe Params: Subscription

Use the same validation as redirect subscription checkout:

- selected plan must be paid and selectable
- reject if active paid entitlement already exists
- ensure or create Stripe customer
- persist checkout session id on user billing state

Stripe session params:

```python
params = {
    "ui_mode": "embedded_page",
    "redirect_on_completion": "never",
    "mode": "subscription",
    "customer": stripe_customer_id,
    "line_items": [
        {
            "price": plan.stripe_price_id,
            "quantity": 1,
        }
    ],
    "client_reference_id": uid,
    "metadata": {
        "firebaseUserId": uid,
        "planKey": plan_key,
        "checkoutUiMode": "embedded",
    },
}
```

Do not include:

- `success_url`
- `cancel_url`
- `return_url`

### Stripe SDK Compatibility

The repo currently uses Stripe SDK compatibility shims in top-up code. Embedded checkout should follow the same `client.checkout.sessions.create(params={...})` style already used in billing modules.

The returned session must expose:

- `session.id`
- `session.client_secret`

If `client_secret` is missing, treat it as a backend error and log `embedded_checkout_missing_client_secret`.

### API: Top-Up Checkout Sync

Add a sync endpoint for top-up checkout completion:

```http
POST /billing/topup-checkout-session/sync
Authorization: Bearer <firebase id token>
Content-Type: application/json
```

Request:

```json
{
  "sessionId": "cs_test_..."
}
```

Response:

```json
{
  "status": "complete",
  "applied": true,
  "topupCredits": {
    "totalRemaining": 15,
    "totalReserved": 0,
    "totalAvailable": 15,
    "activePackCount": 1
  }
}
```

Implementation:

1. Authenticate Firebase user.
2. Retrieve Checkout Session from Stripe.
3. Verify ownership:
   - `client_reference_id == uid`
   - metadata `firebaseUserId == uid`
   - metadata `purchaseType == "topup"`
4. Verify session is paid/complete:
   - `status == "complete"`
   - `payment_status == "paid"`
5. Apply top-up grant through the same idempotent function used by `checkout.session.completed`.
6. Return current aggregate state.

If the session is still open or unpaid, return `409` with a retryable message.

### Subscription Sync

The existing `POST /billing/checkout-session/sync` can be reused if it already:

- verifies session ownership
- verifies it is a subscription checkout
- retrieves/applies the subscription state from Stripe

If it requires only redirect sessions, remove that assumption. Embedded sessions should be accepted if ownership and mode are valid.

### Webhook Handling

No new event type is required.

Existing webhook behavior remains:

- `checkout.session.completed` for top-up applies top-up pack grants.
- subscription events apply paid entitlement and credit refresh metadata.

Required idempotency:

- If sync endpoint applies first, webhook later must no-op safely.
- If webhook applies first, sync endpoint later must no-op safely.

### Logging

Add structured logs:

```text
embedded_checkout_create_start uid=... checkout_type=... key=...
embedded_checkout_created uid=... checkout_type=... session_id=... ui_mode=embedded_page
embedded_checkout_missing_client_secret uid=... checkout_type=... session_id=...
topup_checkout_sync_start uid=... session_id=...
topup_checkout_sync_applied uid=... session_id=... quantity=...
topup_checkout_sync_noop uid=... session_id=... reason=already_applied
topup_checkout_sync_retryable uid=... session_id=... status=... payment_status=...
```

Never log full client secret.

## Frontend Changes

### Dependencies

Add Stripe frontend packages if not already present:

```json
{
  "@stripe/stripe-js": "...",
  "@stripe/react-stripe-js": "..."
}
```

Do not bundle Stripe.js directly. Load through Stripe’s supported loader.

### API Client

Add methods:

```ts
export type EmbeddedCheckoutRequest =
  | { checkoutType: "topup"; packKey: "topup_15" }
  | { checkoutType: "subscription"; planKey: BillingPlanKey };

export type EmbeddedCheckoutResponse = {
  checkoutSessionId: string;
  clientSecret: string;
  checkoutType: "topup" | "subscription";
};

export async function createEmbeddedCheckoutSession(
  body: EmbeddedCheckoutRequest
): Promise<EmbeddedCheckoutResponse>;

export async function syncTopupCheckoutSession(
  sessionId: string
): Promise<TopupCheckoutSyncResponse>;
```

### Paywall Modal State

Extend `BillingPaywallModal`:

```ts
type CheckoutViewState =
  | "plans"
  | "creating_checkout"
  | "embedded_checkout"
  | "confirming"
  | "failed";

type ActiveEmbeddedCheckout = {
  checkoutType: "topup" | "subscription";
  checkoutSessionId: string;
  clientSecret: string;
  planKey?: BillingPlanKey;
  packKey?: "topup_15";
  previousAvailableCredits: number;
  previousActivePlanKey: BillingPlanKey;
};
```

### Starting Checkout

Top-up button:

```ts
await createEmbeddedCheckoutSession({
  checkoutType: "topup",
  packKey: "topup_15",
});
```

Plan button:

```ts
await createEmbeddedCheckoutSession({
  checkoutType: "subscription",
  planKey,
});
```

On success:

- set `activeEmbeddedCheckout`
- switch view to `embedded_checkout`
- track GA event `billing_checkout_embedded_mount`

On failure:

- switch view to `failed`
- show backend error message
- allow return to plan choices

### Mounting Embedded Checkout

Use React Stripe.js Embedded Checkout provider or direct Stripe.js mount.

Provider shape:

```tsx
<EmbeddedCheckoutProvider
  stripe={stripePromise}
  options={{
    clientSecret: activeEmbeddedCheckout.clientSecret,
    onComplete: handleEmbeddedCheckoutComplete,
  }}
>
  <EmbeddedCheckout />
</EmbeddedCheckoutProvider>
```

If current installed React Stripe.js version expects `fetchClientSecret`, use:

```tsx
options={{
  fetchClientSecret: async () => activeEmbeddedCheckout.clientSecret,
  onComplete: handleEmbeddedCheckoutComplete,
}}
```

### Completion Handling

`handleEmbeddedCheckoutComplete` must not close the paywall immediately.

It should:

1. switch view to `confirming`
2. start sync attempt
3. wait for Firestore billing state to reflect the purchase

Top-up confirmation condition:

```ts
billing.availableCredits > active.previousAvailableCredits
```

or:

```ts
billing.topupActivePackCount > previousTopupActivePackCount
```

Subscription confirmation condition:

```ts
billing.activePlanKey === active.planKey
```

or:

```ts
["active", "trialing"].includes(billing.stripeSubscriptionStatus ?? "")
```

When confirmed:

- clear active checkout state
- close modal
- show global info banner:
  - top-up: `Credits added. You can continue rendering.`
  - subscription: `Plan updated. You can continue rendering.`

Do not auto-trigger the blocked action.

### Sync Retry Loop

After `onComplete`:

```ts
const delaysMs = [0, 1000, 2000, 4000, 8000, 15000];
```

For each delay:

- wait delay
- call the appropriate sync endpoint
- stop if confirmation condition becomes true

If all retries complete without confirmation:

- keep modal in `confirming`
- show:
  `Payment received. We are still confirming your credits. Keep this window open or try again in a moment.`
- show a `Retry sync` button
- show a `Back to plans` button only if the session is not confirmed and the user wants to abandon the modal

### Modal Close Rules

While `embedded_checkout`:

- allow close with confirmation prompt:
  `Checkout is in progress. Closing will cancel this checkout view, but your Studio work will remain.`

While `confirming`:

- do not hard-block close, but warn that billing confirmation may still be processing.
- closing the modal must not clear Studio state.

### Existing Redirect Return Handling

Keep existing `/app?checkout=success`, `/app?topup=success`, and `/app?billing=sync` handling for backwards compatibility with sessions already created by old code or billing portal returns.

Do not use that path for new Studio paywall checkout sessions.

### Analytics

Add/keep events:

```ts
trackEvent("billing_checkout_start", {
  checkout_type,
  plan_key,
  pack_key,
  trigger,
  ui_mode: "embedded",
});

trackEvent("billing_checkout_complete_client", {
  checkout_type,
  ui_mode: "embedded",
});

trackEvent("billing_checkout_confirmed", {
  checkout_type,
  ui_mode: "embedded",
});

trackEvent("billing_checkout_timeout", {
  checkout_type,
  ui_mode: "embedded",
});

trackEvent("billing_checkout_failed", {
  checkout_type,
  ui_mode: "embedded",
  reason,
});
```

## UI Copy

Creating:

- `Opening secure checkout...`

Embedded checkout title:

- `Complete checkout`

Confirming top-up:

- `Confirming your credits...`

Confirming subscription:

- `Activating your plan...`

Confirmed top-up banner:

- `Credits added. You can continue rendering.`

Confirmed subscription banner:

- `Plan updated. You can continue rendering.`

Timeout:

- `Payment received. We are still confirming your credits. Keep this window open or try again in a moment.`

## Error Handling

### Checkout Creation Fails

Show backend message in paywall and keep Studio state unchanged.

Examples:

- top-up pack limit reached
- invalid plan
- active subscription already exists
- Stripe unavailable

### Payment Fails Inside Stripe

Stripe iframe handles payment errors. User remains in embedded checkout and can retry.

### Sync Fails

If sync endpoint fails but Firestore listener later updates successfully, still close modal.

If sync returns retryable `409`, continue retry loop.

If sync returns ownership/security error, stop retries and show generic error.

## Security

- Frontend passes only `checkoutType`, `packKey`, or `planKey`.
- Backend owns price IDs, quantity caps, customer IDs, and credit amounts.
- Backend validates Firebase user on every endpoint.
- Sync endpoints verify Stripe session ownership.
- `clientSecret` is only sent to the authenticated user who created the session.
- No redirect payment methods in this phase.

## Test Plan

### Backend Unit Tests

Top-up embedded session:

- creates session with `ui_mode=embedded_page`
- creates session with `redirect_on_completion=never`
- does not include `success_url`, `cancel_url`, or `return_url`
- includes top-up metadata and hold ID
- returns client secret
- enforces max active pack slots

Subscription embedded session:

- creates session with `ui_mode=embedded_page`
- creates session with `redirect_on_completion=never`
- does not include redirect URLs
- includes plan metadata
- rejects invalid/free plan
- rejects if active paid entitlement exists

Top-up sync:

- rejects unauthenticated user
- rejects session owned by another user
- rejects non-top-up session
- returns retryable error for open/unpaid session
- applies completed paid session
- is idempotent if webhook already applied
- no-ops safely if called twice

Subscription sync:

- accepts embedded subscription session
- rejects wrong user/session
- idempotent with webhook

### Frontend Unit/Component Tests

- top-up button calls embedded checkout endpoint, not redirect endpoint
- plan button calls embedded checkout endpoint, not redirect endpoint
- modal renders embedded checkout state
- `onComplete` switches to confirming
- modal closes only after billing state confirms
- modal does not auto-resume blocked action
- timeout state shows retry UI
- Studio state is not cleared when paywall transitions checkout states

### Manual QA

Top-up:

1. Upload score.
2. Generate audio and leave it in multitrack player.
3. Trigger insufficient-credit paywall.
4. Buy top-up through embedded checkout.
5. Verify no full page refresh.
6. Verify score preview remains.
7. Verify multitrack lanes remain.
8. Verify credit balance updates.
9. Verify paywall closes.
10. Manually retry the blocked action.

Subscription:

1. Open paywall from billing menu or low-credit state.
2. Upgrade plan through embedded checkout.
3. Verify no full page refresh.
4. Verify plan and credits update.
5. Verify paywall closes.

Webhook delay:

1. Stop Stripe listener locally.
2. Complete checkout.
3. Verify modal remains in confirming state.
4. Restart listener.
5. Verify modal closes after billing update.

Redirect disabled:

1. Confirm redirect-based payment methods are not presented.
2. Confirm no `return_url` is required for the created embedded session.
3. Confirm a 3DS card test still completes in embedded checkout if supported by Stripe.

## Rollout

### Phase 1

- Implement embedded top-up checkout.
- Keep old redirect top-up endpoint available but unused by Studio.
- Validate in local Stripe sandbox.

### Phase 2

- Implement embedded subscription checkout.
- Keep old redirect subscription endpoint available for backwards compatibility.

### Phase 3

- Remove or deprecate old Studio redirect checkout path after production confidence.
- Keep billing portal redirect behavior unchanged because portal is a separate account-management flow, not a mid-render paywall flow.

## Deployment Notes

- Backend billing service must be deployed for new endpoints.
- Studio frontend must be deployed for embedded checkout UI.
- Stripe webhook endpoint does not need a new event subscription if it already receives `checkout.session.completed` and subscription events.
- Firebase rules should not need changes unless new client-readable checkout records are introduced. This design does not require them.

