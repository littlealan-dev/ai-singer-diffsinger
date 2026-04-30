# SightSinger Billing Paywall UI Design Spec

## 1. Purpose

Define the v1 product UI for Stripe paid-plan adoption in SightSinger.

This spec covers:

- the in-app paywall/pricing surface
- annual/monthly pricing toggle behavior
- plan card content and call-to-action states
- trigger points that open the paywall
- UI flow for each trigger
- frontend/backend integration requirements for Stripe Checkout and Customer Portal

This spec builds on:

- [sightsinger_stripe_billing_spec_v1_final.md](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/docs/specs/sightsinger_stripe_billing_spec_v1_final.md)
- [sightsinger_stripe_billing_lld.md](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/docs/specs/sightsinger_stripe_billing_lld.md)

## 2. Product Goals

1. Replace waitlist-based credit exhaustion prompts with a paid-plan upgrade path.
2. Make plan value clear in terms SightSinger users understand: monthly credits, equivalent audio minutes, and commercial rights.
3. Keep v1 billing simple:
   - Stripe Checkout for new subscriptions.
   - Stripe Customer Portal for existing paid subscribers.
   - No self-serve plan switching in v1.
   - No stacked subscriptions.
4. Preserve free-tier access while clearly explaining the boundary between free and paid usage.

## 3. Paywall Surface

The paywall should be an authenticated in-app modal or full-screen overlay launched from `/app`.

Recommended v1 implementation: modal overlay.

Reasons:

- Most triggers happen inside the studio workflow.
- A modal keeps the user anchored to the score/chat context that caused the upgrade prompt.
- Stripe Checkout already handles the external payment step.

The modal should contain:

- trigger-specific headline, defaulting to `Choose your SightSinger plan`
- trigger-specific supporting copy, defaulting to `Credits refresh monthly. 1 credit is about 30 seconds of generated audio.`
- annual/monthly segmented toggle, defaulting to `Annual`
- three plan cards:
  - Free
  - Solo
  - Choir
- footer links:
  - `Manage Billing` only for users with a Stripe customer ID or active paid billing state
  - `Terms`
  - `Privacy`

## 4. Pricing Toggle

The toggle has two options:

- `Annual`
- `Monthly`

Default state: `Annual`.

Behavior:

- Switching the toggle updates the displayed paid-plan price and selected `PlanKey`.
- The Free card remains `$0/mo` in both modes.
- Annual mode shows the equivalent monthly price, not only the annual invoice total.
- Annual mode also shows a `Save xx%` badge next to each paid price.
- Monthly mode hides annual savings badges.
- The selected toggle must be persisted only in component state for v1. Do not persist it to Firestore.
- The same pricing logic must be shared by the in-app paywall and public marketing pricing page.

### 4.1 Display Pricing

Use the billing catalog from the Stripe billing spec.

| Plan | Monthly invoice | Annual invoice | Annual equivalent monthly | Annual save badge |
| --- | ---: | ---: | ---: | ---: |
| Free | $0/mo | $0/mo | $0/mo | none |
| Solo | $8.99/mo | $89/year | $7.42/mo | Save 18% |
| Choir, early supporter enabled | $19.99/mo | $199/year | $16.58/mo | Save 17% |
| Choir, standard | $24.99/mo | $249/year | $20.75/mo | Save 17% |

Savings formula:

```text
round((monthly_price * 12 - annual_price) / (monthly_price * 12) * 100)
```

Choir display rule:

- If `CHOIR_EARLY_SUPPORTER_ENABLED=true`, show early supporter Choir pricing.
- If `CHOIR_EARLY_SUPPORTER_ENABLED=false`, show standard Choir pricing.
- Existing early supporter subscribers should still see their current plan as current even after the flag is disabled.

## 5. Plan Cards

Cards should follow the reference pattern:

- dark app-compatible cards
- three equal-width pricing banners on desktop
- stacked cards on mobile
- each card has plan name, price, credits, features, and action area
- paid cards should visually read as upgrade options, but should not overpower the studio UI

Card order:

1. Free
2. Solo
3. Choir

### 5.1 Free Card

Content:

- Plan name: `Free`
- Subtitle: `Try SightSinger`
- Price: `$0/mo`
- Credits: `8 credits reset every month`
- Audio equivalent: `About 4 minutes of audio monthly`
- Features:
  - `Full commercial rights`
  - `Upload scores`
  - `Generate AI singing with available credits`
- Action:
  - If current plan is Free: disabled label `Current Plan`
  - If current plan is paid: show no action

### 5.2 Solo Card

Content:

- Plan name: `Solo`
- Subtitle: `For individual creators`
- Price:
  - monthly toggle: `$8.99/mo`
  - annual toggle: `$7.42/mo`, with secondary copy `$89 billed yearly`
- Credits: `30 credits reset every month`
- Audio equivalent: `About 15 minutes of audio monthly`
- Features:
  - `Full commercial rights`
  - `Monthly credit refresh`
  - `Access to current studio voices`
- Action:
  - Free user: `Upgrade to Solo`
  - Solo monthly user viewing monthly: disabled label `Current Plan`
  - Solo annual user viewing annual: disabled label `Current Plan`
  - Any paid user viewing a non-current paid card: `Manage Billing`

Selected plan keys:

- Monthly toggle: `solo_monthly`
- Annual toggle: `solo_annual`

### 5.3 Choir Card

Content:

- Plan name: `Choir`
- Subtitle: `For heavier creation and commercial work`
- Price:
  - early supporter monthly toggle: `$19.99/mo`
  - early supporter annual toggle: `$16.58/mo`, with secondary copy `$199 billed yearly`
  - standard monthly toggle: `$24.99/mo`
  - standard annual toggle: `$20.75/mo`, with secondary copy `$249 billed yearly`
- Credits: `120 credits reset every month`
- Audio equivalent: `About 60 minutes of audio monthly`
- Features:
  - `Full commercial rights`
  - `Monthly credit refresh`
  - `More room for arrangement experiments`
  - `Access to current studio voices`
- Badge:
  - `Best value` or `Most credits`
- Action:
  - Free user: `Upgrade to Choir`
  - Choir monthly user viewing monthly: disabled label `Current Plan`
  - Choir annual user viewing annual: disabled label `Current Plan`
  - Any paid user viewing a non-current paid card: `Manage Billing`

Selected plan keys:

- Early supporter enabled, monthly toggle: `choir_early_monthly`
- Early supporter enabled, annual toggle: `choir_early_annual`
- Early supporter disabled, monthly toggle: `choir_monthly`
- Early supporter disabled, annual toggle: `choir_annual`

## 6. Current Plan State

The UI must derive current plan from `users/{uid}.billing.activePlanKey`.

Required UI states:

- `free`
- `solo_monthly`
- `solo_annual`
- `choir_early_monthly`
- `choir_early_annual`
- `choir_monthly`
- `choir_annual`

Display rules:

- The card matching `activePlanKey` shows a disabled `Current Plan` label.
- Paid users must not see checkout CTAs for other paid plans in v1.
- Paid users viewing non-current paid cards must always see `Manage Billing`.
- Paid users viewing the Free card must see no card action.
- Paid users should see `Manage Billing` for billing changes, invoices, payment method updates, and cancellation.
- If `billing.cancelAtPeriodEnd=true`, show a compact notice: `Cancels at period end`.
- If `stripeSubscriptionStatus=past_due`, show a warning banner in the modal: `Payment issue. Update your payment method to keep paid access.`

## 6.1 Visual Hierarchy

Plan cards should prioritize information in this order:

1. price
2. monthly credits
3. equivalent audio minutes
4. rights and features
5. CTA

On mobile, preserve this hierarchy even when cards stack vertically. Avoid letting long feature lists push the CTA so far down that the price, credits, and action no longer feel connected.

## 7. Paywall Trigger Points

The paywall should open from these points.

Anti-flicker rules:

- Do not auto-open the paywall until both credits and billing snapshots have resolved.
- Auto-open only once per trigger condition per page load.
- Explicit user actions, such as clicking a blocked send button, may reopen the modal after dismissal.

### 7.1 Credits exhausted on studio load

Trigger:

- User opens `/app`.
- Credits snapshot has `available <= 0`.
- `credits.overdrafted=false`.
- Credits and billing state have both finished loading.

Current behavior:

- Opens waitlist modal with `Credits Exhausted`.

New behavior:

1. Open paywall modal automatically after credits and billing state load.
2. Default toggle to `Annual`.
3. Highlight Solo as the recommended plan.
4. Show contextual header copy: `You're out of credits. Upgrade to keep generating.`
5. Upload, drag-and-drop, chat send, and part selection send remain blocked while `available <= 0`.

Do not auto-open this app-load exhaustion paywall while the user still has positive available credits.

### 7.2 Credit overdraft or locked negative balance

Trigger:

- Credits snapshot has `credits.overdrafted=true` or `available < 0`.

Current behavior:

- Locks studio actions and shows a waitlist-style blocked state.

New behavior:

1. Open paywall modal automatically.
2. Show warning copy: `Your account needs attention before more audio can be generated.`
3. If billing has a Stripe customer ID, primary action is `Manage Billing`.
4. If no Stripe customer ID exists, show paid plan CTAs but include support copy: `If this balance looks wrong, contact support before upgrading.`
5. Keep all generation and upload actions blocked until backend state is repaired or credits become positive.

### 7.3 Trial expired legacy state

Trigger:

- `credits.expiresAt` exists and is in the past.

Context:

- v1 billing migrates users to permanent free-tier semantics, but legacy users can still have expired trial state during rollout.

New behavior:

1. Backend migration runs first.
2. User is moved to the permanent Free plan.
3. User receives 8 monthly credits.
4. Open paywall modal after migration completes.
5. Header copy: `Your old trial has been upgraded to the permanent free plan`
6. Supporting copy: `You now receive 8 credits every month. Upgrade any time for more monthly credits.`

If backend migration already refreshed the user to Free before the UI sees the expired state, do not show the legacy-expired framing. If the expired state persists because migration failed, show a retryable error and keep studio actions blocked until credits are refreshed or a paid plan is purchased.

### 7.4 Backend insufficient-credit response

Trigger:

- Any upload, chat, generation, or progress-starting request returns an insufficient-credit billing error.
- Example backend message: `Insufficient credits. This render requires ~{estimated_credits} credits, but you only have {available_credits} available.`

New behavior:

1. Do not silently fail or only show a toast.
2. Open paywall modal.
3. Header copy: `This take needs more credits. Upgrade to continue.`
4. Include the estimate when available: `Estimated cost: {estimated_credits} credits. Available: {available_credits}.`
5. After successful Checkout and webhook refresh, user returns to `/app` and can retry manually.

### 7.5 User presses disabled generation controls

Trigger controls:

- chat send button
- part/verse selector send button
- file upload button
- score drag-and-drop zone

Condition:

- `creditsLocked=true`, where credits are exhausted, expired, or overdrafted.

New behavior:

1. Instead of returning silently, open paywall modal.
2. Use trigger-specific copy:
   - upload: `Upgrade to upload and prepare more scores.`
   - chat send: `Upgrade to generate more singing.`
   - selector send: `Upgrade to render this selected part.`
   - drag-and-drop: `Upgrade to upload this score.`
3. Preserve the user's current text input and score state.

### 7.6 Header credit pill

Trigger:

- User clicks the credits pill.

New behavior:

1. Open paywall modal in all states.
2. If user is free and has credits remaining, show neutral copy: `Compare plans and monthly credits.`
3. If user is low on credits, show: `Running low on credits. Upgrade anytime.`
4. If user is paid, show current plan and `Manage Billing`.

Low-credit threshold:

- `available <= 2` for Free
- `available <= 5` for Solo
- `available <= 15` for Choir

### 7.7 App menu billing entry

Trigger:

- User opens account menu and chooses `Billing` or `Plans`.

New behavior:

1. Open paywall modal.
2. If user is free, show upgrade cards.
3. If user is paid, show current plan and `Manage Billing`.

### 7.8 Landing or marketing pricing CTA

Trigger:

- User views or clicks pricing CTAs on the public marketing page.

New behavior:

1. Public pricing is visible before sign-in.
2. Public pricing uses the same plan-display config and pricing calculations as the in-app paywall.
3. Public pricing does not show current-plan state because the user may be unauthenticated.
4. Public pricing assumes no current plan for display purposes.
5. If unauthenticated user clicks a Free plan CTA, open sign-in and route to `/app` after sign-in.
6. If unauthenticated user clicks a paid plan CTA, open sign-in and preserve the selected `planKey`.
7. After successful sign-in for a preserved paid `planKey`, create the Checkout session directly for that plan.
8. Do not send the user back to the paywall to choose again.
9. After Checkout completion, continue with the normal post-payment sync flow.

Implementation note:

- The marketing pricing page and in-app paywall should share plan-display config, price formatting, savings calculations, and plan-key selection.
- They may use different CTA behavior for unauthenticated public pricing, authenticated in-app free users, and authenticated paid users.

## 7.9 Modal Dismissibility

Dismissible triggers:

- header credit pill
- app menu billing entry
- voluntary pricing exploration
- public marketing pricing after sign-in when no Checkout is in progress

Dismissible behavior:

- close button works
- Escape works
- backdrop click may close if consistent with the app modal pattern

Hard-block or effectively non-dismissible triggers:

- `available <= 0` app-load exhaustion
- overdraft or locked negative balance
- backend insufficient-credit response
- blocked studio control interaction due to locked credits

Hard-block behavior:

- close button may remain available so users are not trapped in the modal.
- Escape and backdrop click should not be the primary dismissal path.
- If the user closes the modal and tries the blocked action again, reopen the paywall with the same trigger context.
- Studio actions remain blocked until credit or billing state allows them.

## 8. Checkout Flow

Checkout is allowed only for free users or users without an active paid entitlement.

Flow:

1. User selects paid card CTA.
2. UI computes `planKey` from selected card and annual/monthly toggle.
3. UI calls billing backend `createCheckoutSession`.
4. Backend returns Stripe Checkout URL.
5. UI redirects browser to Stripe Checkout.
6. On success, Stripe returns user to `STRIPE_CHECKOUT_SUCCESS_URL`.
7. UI shows `Completing your upgrade...` while Firestore billing state updates via webhook.
8. Poll reactive billing state every 2 seconds while also listening to Firestore snapshots.
9. When `billing.activePlanKey` becomes the purchased plan and credits refresh, close modal or show success state.
10. Timeout the post-checkout sync wait after 30 seconds.
11. If state is still unsynced after timeout, show `Payment received. Your plan is still syncing. Refresh in a moment.`
12. Provide a `Refresh status` action that refetches or rechecks the billing state.
13. Allow the user to close the modal after the timeout while the app continues to receive reactive billing updates.

CTA loading behavior:

- Checkout CTA label becomes `Redirecting to Checkout...`.
- Disable only the clicked Checkout CTA during the request.
- Prevent double-submit on the same CTA.
- Keep the interval toggle and other modal content readable.
- The modal close control remains available until browser redirect begins.

Error handling:

- `400 Invalid paid plan`: show `This plan is not available. Refresh and try again.`
- `409 Active paid subscription already exists`: replace CTA with `Manage Billing`.
- network or auth failure: show inline modal error and keep the user in the modal.

## 9. Customer Portal Flow

Portal is used for paid users and for users who already have a Stripe customer ID.

Flow:

1. User clicks `Manage Billing`.
2. UI calls billing backend `createPortalSession`.
3. Backend returns Stripe Customer Portal URL.
4. UI redirects browser to Stripe Customer Portal.
5. Stripe returns user to `STRIPE_PORTAL_RETURN_URL`.
6. UI refreshes Firestore-derived billing display.

CTA loading behavior:

- Portal CTA label becomes `Opening Billing...`.
- Disable only the clicked Portal CTA during the request.
- Prevent double-submit on the same CTA.
- Keep the rest of the modal readable.

Portal should support:

- update payment method
- view invoices
- cancel subscription at period end

Portal must not support in v1:

- self-serve upgrade from Solo to Choir
- self-serve downgrade from Choir to Solo
- switching monthly/annual interval

## 10. Frontend Data Contract

The app UI needs one Firestore-backed hook that combines credits and billing state.

Recommended hook:

```ts
type BillingPlanKey =
  | "free"
  | "solo_monthly"
  | "solo_annual"
  | "choir_early_monthly"
  | "choir_early_annual"
  | "choir_monthly"
  | "choir_annual";

type BillingUiState = {
  activePlanKey: BillingPlanKey;
  family: "free" | "solo" | "choir";
  billingInterval: "none" | "month" | "year";
  stripeSubscriptionStatus?: string | null;
  stripeCustomerId?: string;
  cancelAtPeriodEnd?: boolean;
  currentPeriodEnd?: Date | null;
  nextCreditRefreshAt?: Date | null;
  monthlyAllowance: number;
  availableCredits: number;
  reservedCredits: number;
  loading: boolean;
  error?: string;
};
```

Existing `useCredits()` can be extended or paired with a new `useBilling()` hook.

## 10.1 Shared Plan Display Config

The frontend must use one canonical, client-safe plan display source for:

- in-app paywall modal
- public marketing pricing page
- plan card rendering
- checkout plan-key selection

This shared presenter should derive:

- displayed monthly price
- annual equivalent monthly price
- annual savings badge
- billing interval copy
- monthly credit allowance
- equivalent audio minutes
- selected checkout `PlanKey`
- early supporter Choir display state
- current-plan CTA state when billing state is known

Do not duplicate pricing arithmetic or plan-key mapping across marketing and app UI components.

## 10.2 Billing Load Failure

If billing state fails to load:

- still render base pricing information from the client-safe plan display config
- render no `Current Plan` state
- disable paid Checkout CTAs until auth and billing state are recovered if required to avoid duplicate subscriptions
- show a retryable inline error, for example `Could not load your billing status. Retry.`
- keep public marketing pricing visible, but guard any post-sign-in checkout handoff until billing state is available

## 11. Component Structure

Recommended components:

- `BillingPaywallModal`
- `BillingIntervalToggle`
- `PlanCard`
- `BillingStatusBanner`
- `useBillingState`
- shared plan presenter/config, for example `billingPlansDisplay.ts`
- API helpers:
  - `createCheckoutSession(planKey)`
  - `createPortalSession()`

The existing `WaitlistModal` should no longer be used for credit exhaustion after billing launch. It can remain for non-billing waitlist surfaces if still needed.

## 12. Responsive Behavior

Desktop:

- Modal max width: roughly `1100px`.
- Three cards in one row.
- Toggle centered above cards.

Tablet:

- Cards may use a two-column layout with Choir spanning or stacking as needed.

Mobile:

- Cards stack vertically.
- Toggle remains sticky near top of modal content if the modal scrolls.
- CTA buttons are full width.
- Current-plan state remains visible without needing horizontal scroll.
- Preserve the plan-card hierarchy: price, credits, equivalent minutes, features, CTA.

## 13. Accessibility

Requirements:

- Modal uses `role="dialog"` and `aria-modal="true"`.
- Focus moves into modal on open and returns to the triggering control on close.
- Tab order should be close button, interval toggle, plan CTAs, then footer links.
- Annual/monthly toggle uses buttons with `aria-pressed` or a radiogroup.
- Plan cards use semantic headings.
- Checkout and portal loading states disable only the affected CTA.
- Price and savings copy must be text, not image-only.
- Disabled `Current Plan` state must remain readable and must not behave like a broken focus target.
- Price, annual billing copy, savings badges, credits, and equivalent minutes must remain screen-reader readable.

## 14. Analytics Events

Recommended events:

- `billing_paywall_opened`
  - properties: `trigger`, `activePlanKey`, `availableCredits`
- `billing_interval_toggled`
  - properties: `interval`
- `billing_checkout_clicked`
  - properties: `planKey`, `trigger`
- `billing_checkout_redirected`
  - properties: `planKey`
- `billing_checkout_error`
  - properties: `planKey`, `statusCode`
- `billing_portal_clicked`
  - properties: `activePlanKey`
- `billing_paywall_closed`
  - properties: `trigger`

Do not rely on Firebase Analytics-triggered Cloud Functions for billing logic.

## 15. Acceptance Criteria

1. Annual/monthly toggle defaults to Annual and updates Solo/Choir prices and `PlanKey`.
2. Annual mode shows equivalent monthly price plus savings badge for paid plans.
3. Free, Solo, and Choir cards show monthly credits and equivalent audio minutes.
4. Free, Solo, and Choir cards show `Full commercial rights`.
5. Current plan card shows disabled `Current Plan`.
6. Paid users cannot start a second checkout session from the UI.
7. Paid users on non-current paid cards see `Manage Billing`.
8. Paid users see no action on the Free card.
9. Credit exhaustion, overdraft, legacy trial migration, backend insufficient-credit errors, disabled studio controls, credits pill, app menu billing, and marketing CTA can all open the paywall with appropriate contextual copy.
10. App-load auto-open waits for credits and billing state, opens only for `available <= 0`, and opens only once per trigger condition per page load.
11. Upload, drag-and-drop, and generation controls remain blocked at zero or locked-credit states.
12. Free-user paid card CTA redirects to Stripe Checkout.
13. Paid-user billing action redirects to Stripe Customer Portal.
14. Checkout return polls or observes billing state every 2 seconds, times out after 30 seconds, and shows a `Refresh status` fallback.
15. Public marketing pricing is visible before sign-in, preserves selected paid plan through sign-in, and can continue directly to Checkout.
16. Marketing pricing and in-app paywall share one plan-display config and pricing presenter.
17. Returning from Checkout or Portal refreshes the Firestore-derived billing state without requiring a full browser restart.
