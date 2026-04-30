# SightSinger Billing Paywall UI Design Spec — Review Comments

## Overall assessment

The UI design spec is strong, product-aware, and close to implementation-ready. It has a solid trigger inventory, clear paid-vs-portal separation, and a good pricing-card structure. The main recommended updates are about tightening product copy, reducing ambiguity in a few CTA states, and clarifying several operational UX flows.

This review incorporates the additional product decisions provided afterward.

---

## Agreed changes to apply

## 1. Free card should include full commercial rights
The earlier review flagged a possible conflict around the Free card saying only “Personal, non-commercial exploration.”

### Updated recommendation
The Free card should also include:

- `Full commercial rights`

This change reflects the current product/licensing direction and avoids incorrectly implying that the free tier is non-commercial by default.

### Result
Remove the old “Personal, non-commercial exploration” wording and align the Free card rights language with the actual platform policy.

---

## 2. Remove “priority over free-tier capacity”
The Solo card currently includes:

- `Priority over free-tier capacity when applicable`

### Updated recommendation
Remove this line.

Reason:
- no queueing or priority-capacity behavior has been implemented
- this would create an expectation the backend cannot currently guarantee

### Result
Do not mention queue priority in v1 plan cards.

---

## 3. Legacy expired trial flow should migrate first, then show paywall
The earlier review suggested the “legacy trial expired” state may become temporary and migration-only.

### Updated recommendation
When a legacy expired trial user logs in after the billing patch launches:

1. migrate the user immediately to permanent free plan
2. immediately grant 8 monthly credits
3. then show the paywall
4. the modal copy should explain that the account has now been upgraded to the permanent free plan

Suggested header copy:
- `Your account is now on the permanent free plan`
- or
- `Your old trial has been upgraded to the permanent free plan`

Suggested supporting copy:
- `You now receive 8 credits every month. Upgrade any time for more monthly credits.`

### Result
The legacy expired trial trigger remains valid as a transitional UX, but it should be framed as:
- migration completed
- permanent free plan granted
- upsell shown afterward

This is better than leaving the user in an expired/trial-blocked feeling.

---

## 4. Paid users on non-current paid cards should show `Manage Billing`
The earlier review asked to choose one consistent CTA.

### Updated recommendation
Use exactly:

- `Manage Billing`

for paid users on non-current paid cards.

Do not use:
- `Contact support`

as the primary card CTA for v1.

### Result
This makes the UI simpler and consistent with the no-self-serve-plan-switching rule.

---

## 5. Free card action for paid users should show nothing
The earlier spec allowed:
- `Included fallback plan`
- or no action

### Updated recommendation
Show nothing at all on the Free card for paid users.

### Result
This is the cleanest choice and avoids unnecessary clutter.

---

## 6. App-load trigger should only auto-open for `available <= 0`
### Updated recommendation
Keep the automatic app-load paywall open only for:

- `available <= 0`

Do not expand the automatic app-load trigger beyond that.

### Result
This preserves a strong exhaustion trigger while avoiding overly aggressive auto-open behavior for users who still have credits.

---

## 7. Keep upload blocked at zero/locked-credit states
The earlier review suggested reconsidering whether upload should remain blocked.

### Updated recommendation
Keep the stronger gating:

- upload remains blocked
- drag-and-drop remains blocked
- generation/send actions remain blocked

when the relevant locked-credit state applies.

### Result
The current “upload also blocked” behavior remains intentional and should stay in the spec.

---

## 8. Add explicit checkout-sync timeout and polling behavior
The earlier review suggested defining polling cadence and fallback behavior after returning from Stripe Checkout.

### Updated recommendation
Add explicit rules such as:

- after returning from Checkout, show `Completing your upgrade...`
- poll Firestore/backend every **2–3 seconds**
- timeout after **15–30 seconds**
- if still unsynced, show:
  - `Payment received. Your plan is still syncing. Refresh in a moment.`
- provide a user-visible action such as:
  - `Refresh status`

Optional fallback action:
- allow the user to close the modal and continue waiting for sync, while the app keeps billing state reactive

### Result
This makes the post-checkout UX deterministic and avoids a vague “short timeout” implementation.

---

## 9. Marketing pricing page should show plans before sign-in
The earlier review suggested routing unauthenticated users to sign-in before seeing the paywall/pricing flow.

### Updated recommendation
Do **not** require sign-in before the user can see pricing.

The public marketing pricing surface should display the same pricing information as the in-app paywall, with these differences:

- current plan state is unknown
- assume no current plan
- show action buttons on all cards such as:
  - `Sign in`
  - `Upgrade`
  - `Unlock`
  - final wording can be chosen by design/product

### Required flow
When an unauthenticated user clicks any plan button on the public pricing page:

1. open the sign-in dialog
2. after successful sign-in:
   - if the clicked plan was a paid plan, redirect directly into Checkout session creation for that selected plan
   - do **not** send the user back to the paywall to choose again
3. after Checkout completion, continue with the normal post-payment sync flow

### Result
This is a better acquisition flow:
- users can compare pricing without friction
- sign-in happens at intent moment
- chosen plan selection is preserved across sign-in
- paid checkout can continue immediately after authentication

### Additional implementation note
The marketing pricing page and in-app paywall should share the same plan-display config and pricing logic where possible, while allowing different CTA behavior for:
- unauthenticated public pricing
- authenticated in-app pricing

---

## Other agreed lower-priority comments that should still be applied

## 10. Add one canonical frontend plan-display source
The UI should not hardcode plan pricing and savings logic in multiple places.

### Recommendation
Use one shared client-safe config/presenter for:
- marketing pricing page
- in-app paywall modal
- plan card rendering

This config should derive:
- displayed price
- annual equivalent monthly
- savings badge
- plan key for checkout
- early supporter choir display logic

---

## 11. Add explicit CTA loading and disabled states
Define visible loading states for:
- Checkout CTA: `Redirecting to Checkout...`
- Portal CTA: `Opening Billing...`

And define whether:
- interval toggle remains interactive
- modal close remains interactive
- only the clicked CTA is disabled

Recommendation:
- disable only the clicked CTA during request
- keep other modal UI readable
- prevent double-submit on the same CTA

---

## 12. Define modal dismissibility rules by trigger type
The spec should distinguish between:

### Dismissible triggers
Examples:
- clicking credits pill
- app menu billing
- voluntary pricing exploration
- marketing pricing flow after sign-in

### Hard-block / effectively non-dismissible triggers
Examples:
- available credits <= 0 on app load
- overdraft / locked negative balance
- backend insufficient-credit hard block
- blocked studio control interaction due to locked credits

### Recommendation
Document whether each trigger type:
- can close with Escape
- can close by clicking backdrop
- reopens if user tries blocked action again

This is important for UX consistency.

---

## 13. Clarify visual hierarchy of plan cards
To avoid clutter, define a visual priority order:

1. price
2. monthly credits
3. equivalent minutes
4. rights/features
5. CTA

This is especially important on mobile where cards will stack vertically.

---

## 14. Add fallback UI for billing-state load failure
The current data contract includes `loading`, but the paywall should also define a degraded/error state.

### Recommendation
If billing state fails to load:
- still render base pricing information
- render no “Current Plan” state
- disable or guard paid actions until auth/billing state is recovered if needed
- show retryable inline error when appropriate

This matters for both:
- in-app paywall
- marketing-to-sign-in-to-checkout handoff

---

## 15. Add anti-flicker rule for paywall auto-open
The app should not auto-open the modal until the required state is resolved.

### Recommendation
Add:
- do not auto-open until credits and billing snapshot are loaded
- only auto-open once per trigger condition per page load unless explicitly re-triggered

This prevents annoying modal flicker during auth/initial data loading.

---

## 16. Accessibility additions
The current accessibility section is already good.

Add:
- clear tab order expectations:
  - close button
  - interval toggle
  - plan CTAs
  - footer links
- disabled `Current Plan` state must remain readable and not behave like a broken focus target
- pricing and savings information must remain screen-reader readable

---

## Recommended edits by section

## Section 3 — Paywall Surface
Keep the overall modal recommendation, but ensure:
- modal can support trigger-specific headers
- footer `Manage Billing` link appears only when it makes sense for current billing state

## Section 4 — Pricing Toggle
Keep as-is, but ensure:
- same pricing logic is shared between in-app and public pricing page

## Section 5 — Plan Cards
Apply these exact updates:

### Free Card
- add `Full commercial rights`
- remove any non-commercial-only wording
- for paid users, show no action at all

### Solo Card
- remove `Priority over free-tier capacity when applicable`

### Choir Card
- keep badge like `Best value` or `Most credits`

## Section 6 — Current Plan State
For paid users on non-current paid cards:
- always show `Manage Billing`

## Section 7 — Trigger Points
Apply these updates:

### 7.1 Credits exhausted on studio load
- auto-open only when `available <= 0`

### 7.3 Trial expired legacy state
Change to:
- migrate first
- grant permanent free-tier credits
- then show paywall explaining the user is now on the permanent free plan

### 7.8 Landing or marketing pricing CTA
Replace the current auth-first pricing flow with:
- public pricing visible without sign-in
- sign-in only when CTA is clicked
- preserve chosen plan across sign-in
- redirect directly to Checkout after sign-in for selected paid plan

## Section 8 — Checkout Flow
Add explicit:
- polling every 2–3 seconds
- timeout after 15–30 seconds
- fallback sync message
- optional refresh-status action

## Section 9 — Customer Portal Flow
Keep as-is.

## Section 10 / 11 — Frontend Contract and Component Structure
Add:
- shared pricing/presenter config
- support for public pricing CTA behavior
- support for degraded load/error state

---

## Highest-priority changes before implementation

1. Add **Full commercial rights** to Free card and remove conflicting non-commercial wording
2. Remove **priority queue/capacity** wording from Solo
3. Refine **legacy expired trial** UX to migrate first, then show paywall
4. Standardize paid-user non-current paid-card CTA to **Manage Billing**
5. Make Free card show **no action** for paid users
6. Keep app-load auto-open only for **available <= 0**
7. Keep **upload blocked**
8. Define **checkout sync polling + timeout + fallback**
9. Redesign public **marketing pricing page** flow so pricing is visible before sign-in and selected plan is preserved through sign-in

---

## Final recommendation

After these edits, the UI design spec should be in very good shape for implementation.

The structure is already strong. The main work is now aligning the copy and flow details with the final billing/migration decisions and tightening a few UX behaviors so the frontend team does not have to infer them.
