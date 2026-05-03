# SightSinger Stripe Billing Test Matrix

## 1. Purpose

Define the billing scenarios SightSinger should test before launch.

This matrix assumes the app persists the following Stripe state fields on `users/{userId}.billing`:

```text
latestInvoiceStatus
latestPaymentIntentStatus
latestCheckoutSessionStatus
latestCheckoutPaymentStatus
latestPaymentFailureCode
latestPaymentFailureMessage
latestDisputeId
latestDisputeStatus
latestDisputeReason
latestDisputeCreatedAt
```

Existing billing fields are also assumed:

```text
activePlanKey
billingInterval
stripeCustomerId
stripeSubscriptionId
stripeSubscriptionStatus
cancelAtPeriodEnd
canceledAt
currentPeriodStart
currentPeriodEnd
creditRefreshAnchor
lastCreditRefreshAt
nextCreditRefreshAt
latestInvoiceId
latestInvoicePaidAt
latestInvoicePaymentFailedAt
refreshScheduler.lastAttemptAt
refreshScheduler.lastStatus
refreshScheduler.lastErrorMessage
refreshScheduler.lastRunId
```

## 2. Guiding Rules

Stripe is the source of truth for payment and subscription state.

SightSinger grants credits only through:

- `invoice.paid` for monthly paid subscriptions
- scheduler refresh for free plans
- scheduler refresh for annual paid plans
- scheduler repair when a paid monthly invoice was recorded but not granted because credits were reserved

The scheduler must not silently downgrade a paid monthly user to free because invoice metadata is missing or delayed.

Disputes and refunds are managed in Stripe Dashboard for v1. SightSinger does not send separate support alerts for these events unless a later admin workflow is added.

## 3. Status Reference

### 3.1 Subscription Statuses

```text
incomplete
incomplete_expired
trialing
active
past_due
canceled
unpaid
paused
```

### 3.2 Checkout Session Statuses

```text
open
complete
expired
```

### 3.3 Checkout Payment Statuses

```text
paid
unpaid
no_payment_required
```

### 3.4 Invoice Statuses

```text
draft
open
paid
uncollectible
void
```

### 3.5 PaymentIntent Statuses

```text
requires_payment_method
requires_confirmation
requires_action
processing
requires_capture
canceled
succeeded
```

### 3.6 Dispute Statuses

```text
warning_needs_response
warning_under_review
warning_closed
needs_response
under_review
won
lost
```

## 4. Checkout Scenarios

| ID | Scenario | Stripe input | Expected billing state | Expected credits | Expected user experience |
|---|---|---|---|---|---|
| C-01 | New checkout session created | App creates Checkout Session for Solo monthly | `stripeCustomerId` set or reused; `stripeCheckoutSessionId` set; no paid plan yet | unchanged | User is redirected to Stripe Checkout |
| C-02 | Checkout completes with paid subscription | `checkout.session.completed`; session `status=complete`; `payment_status=paid`; subscription `active`; invoice paid | `activePlanKey=solo_monthly`; `stripeSubscriptionStatus=active`; `latestCheckoutSessionStatus=complete`; `latestCheckoutPaymentStatus=paid`; `latestInvoiceStatus=paid` | grant Solo monthly allowance once; `lastGrantInvoiceId` set | User returns to studio with upgraded plan and refreshed credits |
| C-03 | Checkout completes with annual paid subscription | session complete; payment paid; subscription active; annual price | `activePlanKey=solo_annual` or Pro annual; `billingInterval=year`; `nextCreditRefreshAt` set from Stripe period/anchor | initial paid credits granted once from invoice | Studio shows paid plan; future monthly credits come from scheduler |
| C-04 | Checkout complete with no payment required | session `status=complete`; `payment_status=no_payment_required` | no paid status change for v1 | no credit grant | No UI prompt or warning |
| C-05 | Checkout abandoned | session remains `open` | no paid plan; checkout session may remain stored | unchanged | User can retry checkout |
| C-06 | Checkout expired | session `status=expired` | no paid plan; optional audit only | unchanged | No special UI change; user can start checkout again |
| C-07 | Checkout complete but payment unpaid | session `status=complete`; `payment_status=unpaid`; subscription may be `incomplete` | `latestCheckoutPaymentStatus=unpaid`; subscription status mirrored if available | no credit grant | UI waits for payment completion or asks user to resolve payment |
| C-08 | Checkout session belongs to another user | returned `session_id` metadata/client reference mismatch | reject sync; no billing mutation | unchanged | Show checkout sync error |

## 5. Initial Payment and Authentication Scenarios

| ID | Scenario | Stripe input | Expected billing state | Expected credits | Expected user experience |
|---|---|---|---|---|---|
| P-01 | Initial card payment succeeds | invoice `paid`; PaymentIntent `succeeded` | `latestInvoiceStatus=paid`; `latestPaymentIntentStatus=succeeded`; subscription active | paid allowance granted once | Plan active |
| P-02 | Initial card requires 3DS/authentication | PaymentIntent `requires_action`; subscription `incomplete` | `stripeSubscriptionStatus=incomplete`; `latestPaymentIntentStatus=requires_action` | no grant | User must complete authentication |
| P-03 | Initial payment processing | PaymentIntent `processing`; checkout payment `unpaid` | `latestPaymentIntentStatus=processing` | no grant until `invoice.paid` | UI shows pending/syncing state |
| P-04 | Initial payment fails | PaymentIntent `requires_payment_method`; invoice `open` or failed payment event | `latestPaymentIntentStatus=requires_payment_method`; `latestPaymentFailureCode` set | no grant | User must update payment method |
| P-05 | Initial subscription expires incomplete | subscription `incomplete_expired`; invoice `void` | `stripeSubscriptionStatus=incomplete_expired`; `latestInvoiceStatus=void`; active plan remains or reverts free per sync policy | no grant | User remains free and can retry checkout |
| P-06 | Radar blocks initial payment | card error / payment failure | payment failure fields set | no grant | User sees payment failed |

## 6. Monthly Subscription Renewal Scenarios

| ID | Scenario | Stripe input | Expected billing state | Expected credits | Expected user experience |
|---|---|---|---|---|---|
| M-01 | Monthly renewal paid | `invoice.paid` for monthly price | `latestInvoiceId` set; `latestInvoiceStatus=paid`; `latestInvoicePaidAt` set; subscription status active-ish | monthly allowance granted once; `lastGrantInvoiceId=latestInvoiceId`; `nextCreditRefreshAt` advanced | User sees refreshed paid credits |
| M-02 | Duplicate `invoice.paid` | same invoice event delivered again | event/idempotency records show already processed | no duplicate grant | No visible change |
| M-03 | Monthly renewal paid while credits reserved | `invoice.paid`; `credits.reserved > 0` | invoice metadata recorded; `nextCreditRefreshAt` remains or moves due | no immediate grant | User remains due; scheduler retries |
| M-04 | Scheduler repairs deferred monthly grant | monthly paid user due; `latestInvoiceId != credits.lastGrantInvoiceId`; `reserved=0` | `refreshScheduler.lastStatus=applied` | monthly allowance granted once | Credits refresh after reserved credits clear |
| M-05 | Monthly due but invoice not recorded yet | monthly paid user due; no new `latestInvoiceId`; subscription active-ish | `refreshScheduler.lastStatus=waiting_for_invoice`; no downgrade | no grant; no free reset | User remains on paid plan; debug status visible |
| M-06 | Monthly renewal payment failed | `invoice.payment_failed`; subscription `past_due` | `latestInvoiceStatus=open` or payment failure status; `latestInvoicePaymentFailedAt` set; failure code/message set; subscription status mirrored | no grant | UI warns the user to Manage Billing to avoid service interruption; no automatic free reset |
| M-07 | Monthly subscription becomes unpaid | subscription `unpaid` | `stripeSubscriptionStatus=unpaid` | no new paid grant unless policy allows; no free reset under paid key | UI warns the user to Manage Billing to avoid service interruption |
| M-08 | Monthly subscription paused | subscription `paused` | `stripeSubscriptionStatus=paused` | no paid grant; no free reset under paid key | UI warns the user to Manage Billing to avoid service interruption |

## 7. Annual Subscription Monthly Refresh Scenarios

| ID | Scenario | Stripe input | Expected billing state | Expected credits | Expected user experience |
|---|---|---|---|---|---|
| A-01 | Annual subscription initial invoice paid | `invoice.paid`; annual price | annual active plan; `billingInterval=year`; anchor set | initial paid allowance granted once | User upgraded |
| A-02 | Annual monthly scheduler refresh due | `billing.nextCreditRefreshAt <= now`; annual paid plan active-ish; `reserved=0` | `lastCreditRefreshAt` updated; `nextCreditRefreshAt` advanced; `refreshScheduler.lastStatus=applied` | annual plan monthly allowance granted | User sees refreshed credits |
| A-03 | Annual refresh due with reserved credits | annual paid user due; `reserved > 0` | `refreshScheduler.lastStatus=reserved`; `nextCreditRefreshAt` unchanged | no grant | Scheduler retries later |
| A-04 | Annual refresh after reserved clears | same user; `reserved=0` | `refreshScheduler.lastStatus=applied`; next reset advanced | annual allowance granted | Credits restored |
| A-05 | Annual subscription past_due/unpaid before renewal | subscription status changes near annual renewal | status mirrored | no monthly refresh if policy blocks unpaid; otherwise preserve entitlement until clear policy says revoke | UI warns billing issue |

## 8. Free Plan Refresh Scenarios

| ID | Scenario | Stripe input | Expected billing state | Expected credits | Expected user experience |
|---|---|---|---|---|---|
| F-01 | Free monthly refresh due | `activePlanKey=free`; `nextCreditRefreshAt <= now`; `reserved=0` | `lastCreditRefreshAt` updated; `nextCreditRefreshAt` advanced; `refreshScheduler.lastStatus=applied` | free allowance granted | Free credits restored |
| F-02 | Free refresh due with reserved credits | free user due; `reserved > 0` | `refreshScheduler.lastStatus=reserved`; `nextCreditRefreshAt` unchanged | no grant | Scheduler retries later |
| F-03 | Free refresh duplicate/idempotent replay | deterministic ledger already exists | `refreshScheduler.lastStatus=already_applied`; next refresh metadata repaired if needed | no duplicate grant | No visible duplicate credits |
| F-04 | Free user not due | direct user refresh call but `nextCreditRefreshAt > now` | no meaningful scheduler mutation preferred | no grant | No visible change |

## 9. Cancellation Scenarios

| ID | Scenario | Stripe input | Expected billing state | Expected credits | Expected user experience |
|---|---|---|---|---|---|
| X-01 | User schedules cancellation at period end | `customer.subscription.updated`; `cancel_at_period_end=true` or `cancel_at` set | `cancelAtPeriodEnd=true`; subscription remains active; `currentPeriodEnd` set | no immediate credit change | UI says cancels at period end |
| X-02 | Scheduled cancel before period end and monthly plan due | paid monthly due but subscription still active until period end | do not downgrade; monthly invoice rules still apply | grant only on paid invoice | User keeps entitlement through paid period |
| X-03 | Scheduled cancel reaches period end | `customer.subscription.deleted` or status `canceled` | active plan reverts to free; subscription id cleared | preserve current balance unless policy says reset immediately | UI shows Free plan |
| X-04 | Immediate cancellation | `customer.subscription.deleted`; status `canceled` | active plan free; subscription id cleared; customer id preserved | no paid grant after cancel | User sees Free plan; Billing menu still available if customer id exists |
| X-05 | Subscription incomplete expired | status `incomplete_expired` | free/no paid entitlement | no grant | User can retry checkout |

## 10. Plan Change Scenarios

| ID | Scenario | Stripe input | Expected billing state | Expected credits | Expected user experience |
|---|---|---|---|---|---|
| PC-01 | Solo monthly to Pro monthly | subscription updated to Pro price; invoice paid for change if Stripe generates one | `activePlanKey=choir_early_monthly` or Pro key; status active | grant only when paid invoice event arrives | UI shows new plan after sync |
| PC-02 | Pro monthly to Solo monthly | subscription updated to Solo price | plan mirror updates | no immediate credit clawback unless policy says so | UI shows new plan |
| PC-03 | Monthly to annual | subscription updated to annual price; invoice paid | `billingInterval=year`; anchor updated | initial annual paid grant from invoice; future monthly scheduler | UI shows annual plan |
| PC-04 | Annual to monthly | subscription updated to monthly price | `billingInterval=month` | monthly invoices own future refresh | UI shows monthly plan |
| PC-05 | Plan update payment fails | update creates invoice/payment failure | plan should not be upgraded unless subscription/payment state confirms | no grant | UI shows billing issue |

## 11. Refund Scenarios

Refunds are managed in Stripe Dashboard for v1. Customers cannot request refunds through the standard Stripe Customer Portal.

| ID | Scenario | Stripe input | Expected billing state | Expected credits | Expected user experience |
|---|---|---|---|---|---|
| R-01 | Full refund for latest invoice | refund created from Stripe Dashboard or API | no app-side handling required for v1 unless subscription also changes | no automatic credit clawback in v1 | Managed in Stripe Dashboard |
| R-02 | Partial refund | partial refund created from Stripe Dashboard or API | no app-side handling required for v1 | no automatic credit change | Managed in Stripe Dashboard |
| R-03 | Refund plus subscription cancellation | refund event plus subscription deleted/canceled | cancellation handler reverts billing to free | no future paid refresh | User becomes Free |

## 12. Dispute and Inquiry Scenarios

Disputes are outside the normal payment success path, but inside billing operations.

V1 policy: use Stripe Dashboard dispute management. Do not automatically revoke credits on dispute creation unless Stripe also changes the subscription state.

| ID | Scenario | Stripe input | Expected billing state | Expected credits | Expected user experience |
|---|---|---|---|---|---|
| D-01 | Charge dispute created | `charge.dispute.created`; dispute `status=needs_response` | no app-side handling required for v1 | no automatic credit change | Managed in Stripe Dashboard |
| D-02 | Charge inquiry opened | `charge.dispute.created`; dispute `status=warning_needs_response` | no app-side handling required for v1 | no automatic credit change | Managed in Stripe Dashboard |
| D-03 | Dispute under review | `charge.dispute.updated`; `status=under_review` | no app-side handling required for v1 | no automatic credit change | Managed in Stripe Dashboard |
| D-04 | Dispute won | `charge.dispute.closed`; `status=won` | no app-side handling required for v1 | no automatic credit change | Managed in Stripe Dashboard |
| D-05 | Dispute lost | `charge.dispute.closed`; `status=lost` | no app-side handling required for v1 unless subscription/payment state changes separately | no automatic credit change in v1 | Managed in Stripe Dashboard |
| D-06 | Inquiry won | dispute `status=warning_closed` | no app-side handling required for v1 | no automatic credit change | Managed in Stripe Dashboard |
| D-07 | Inquiry escalates to dispute | `charge.dispute.updated`; status moves to `needs_response`; funds withdrawn event may arrive | no app-side handling required for v1 | no automatic credit change | Managed in Stripe Dashboard |

## 13. Webhook and Idempotency Scenarios

| ID | Scenario | Stripe input | Expected billing state | Expected credits | Expected user experience |
|---|---|---|---|---|---|
| W-01 | Duplicate Stripe event | same event id delivered twice | second delivery ignored by `stripe_events` idempotency | no duplicate grant | No visible change |
| W-02 | Same invoice appears in multiple paths | checkout sync and webhook both process same invoice | deterministic invoice ledger prevents duplicate credit grant | no duplicate grant | No visible change |
| W-03 | Webhook arrives before checkout redirect sync | webhook updates Firestore first | checkout sync sees already-updated/ledger state | no duplicate grant | UI resolves quickly |
| W-04 | Checkout redirect sync arrives before webhook | checkout sync updates Firestore first | webhook later idempotent by invoice ledger/event | no duplicate grant | UI resolves quickly |
| W-05 | Unknown price id | invoice/subscription has unconfigured price | no plan change; warning logged | no grant | Admin investigation |
| W-06 | Missing user resolution | event has no metadata and unknown customer id | event audit written; handler returns conflict/error | no grant | Admin investigation |

## 14. Scheduler Scenarios

| ID | Scenario | Input state | Expected billing state | Expected credits | Expected user experience |
|---|---|---|---|---|---|
| S-01 | No due users | no user with `nextCreditRefreshAt <= now` | no mutations | no change | No visible change |
| S-02 | Batch limit reached | more than 300 due users | first 300 scanned by oldest `nextCreditRefreshAt`; `has_more_due_users=true` | only processed users change | Next run continues |
| S-03 | Per-user failure | one user transaction raises | failed user gets `refreshScheduler.lastStatus=failed`; run continues | other users process | Admin can inspect user |
| S-04 | Monthly paid waiting for invoice | monthly paid active-ish; due; no new paid invoice | `refreshScheduler.lastStatus=waiting_for_invoice`; `nextCreditRefreshAt` remains due or retry policy field set | no free reset; no paid grant | User remains paid; admin can inspect |
| S-05 | Billing state inconsistent | active paid key but terminal subscription status | `refreshScheduler.lastStatus=billing_state_inconsistent` | no grant | Admin sync required |
| S-06 | Metrics emitted | scheduler run completes | Cloud Monitoring metrics emitted | no direct credit effect | Ops dashboards update |

## 15. UI Scenarios

| ID | Scenario | Input state | Expected UI |
|---|---|---|---|
| UI-01 | Free user has credits | `activePlanKey=free`; balance > 0 | Credit pill shows balance and days to reset |
| UI-02 | Paid user has credits | paid plan; balance > 0 | Credit pill shows balance and days to reset |
| UI-03 | Credits exhausted | available credits <= 0 | Paywall opens with exhausted-credit copy |
| UI-04 | Paid plan active | paid plan | Profile menu shows plan badge |
| UI-05 | User has Stripe customer id but currently free | `stripeCustomerId` exists; plan free | Billing menu opens Stripe Customer Portal |
| UI-06 | Scheduled cancel | `cancelAtPeriodEnd=true` | Billing UI says cancels at period end |
| UI-07 | Payment failed/past_due/unpaid/paused | latest payment failure fields or subscription status set | UI asks the user to Manage Billing to avoid service interruption |

## 16. Minimum Launch Test Set

The minimum practical v1 test set should include:

1. Checkout success for Solo monthly.
2. Checkout success for Solo annual.
3. Monthly `invoice.paid` renewal grants credits once.
4. Duplicate `invoice.paid` does not double grant.
5. `invoice.payment_failed` records failure and does not grant.
6. Monthly paid due with no new invoice gets `waiting_for_invoice`, not free reset.
7. Annual paid scheduler refresh grants monthly credits.
8. Free scheduler refresh grants free credits.
9. Scheduler skips reserved credits and later applies after reserved clears.
10. Scheduled cancel keeps paid entitlement until period end.
11. Immediate cancel reverts billing mirror to free.
12. Checkout redirect sync and webhook race does not double grant.
13. Dispute created does not automatically revoke credits; dispute is managed in Stripe Dashboard.
14. Refund is managed in Stripe Dashboard and does not silently corrupt credits.
15. Profile Billing menu opens Stripe portal for users with `stripeCustomerId`.
