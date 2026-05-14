# Paid Plan Launch Checklist

Use this checklist for the first production launch of SightSinger paid plans.

This launch uses three backend surfaces:

- Main app backend: `sightsinger-api`, GPU-enabled Cloud Run, synthesis and app APIs.
- Billing backend: `sightsinger-billing-api`, CPU-only Cloud Run, `/billing/**`.
- Billing scheduler: Firebase Functions `refreshCredits`, recurring credit reset job.

## 1. Pre-Launch Freeze

- [ ] Confirm root repo changes are committed.
- [ ] Confirm marketing repo changes are committed.
- [ ] Confirm production Stripe products and prices match `env/prod.env`.
- [ ] Confirm `STRIPE_API_VERSION` in `env/prod.env` matches the production webhook endpoint API version.
- [ ] Confirm Terms of Use page is live.
- [ ] Confirm Privacy Policy page is live.
- [ ] Confirm Credits / AI Voice Permissions page is live.
- [ ] Confirm pricing page is live and uses live plan links.
- [ ] Confirm FAQ includes subscription, commercial-use, and royalty-free answers.
- [ ] Confirm `env/voicebank_manifest.prod.json` enables only commercial-use voicebanks:
  - `PM-31_Commercial_Indigo`
  - `PM-31_Commercial_Scarlet`
  - `Qixuan_v2.7.0_DiffSinger_OpenUtau`
- [ ] Confirm `Qixuan_v2.7.0_DiffSinger_OpenUtau.tar.gz` is uploaded to the production voicebank bucket path expected by the manifest.

## 2. Stripe Secrets

- [ ] Create or update Secret Manager secret `STRIPE_SECRET_KEY`.
- [ ] Create or update Secret Manager secret `STRIPE_WEBHOOK_SECRET`.
- [ ] Grant secret access to the Cloud Run runtime service account:

```bash
gcloud secrets add-iam-policy-binding STRIPE_SECRET_KEY \
  --project=sightsinger-app \
  --member="serviceAccount:sightsinger-cloud-run-sa@sightsinger-app.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud secrets add-iam-policy-binding STRIPE_WEBHOOK_SECRET \
  --project=sightsinger-app \
  --member="serviceAccount:sightsinger-cloud-run-sa@sightsinger-app.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

- [ ] Confirm `env/prod.env` references secret names, not raw secret values:
  - `STRIPE_SECRET_KEY_SECRET=STRIPE_SECRET_KEY`
  - `STRIPE_WEBHOOK_SECRET_SECRET=STRIPE_WEBHOOK_SECRET`

## 3. Stripe Webhook Endpoint

Deploy the billing backend before setting the final Stripe webhook URL, because the generated Cloud Run URL is only known after the first deployment.

- [ ] Deploy `sightsinger-billing-api`.
- [ ] Get the billing backend URL:

```bash
gcloud run services describe sightsinger-billing-api \
  --project=sightsinger-app \
  --region=us-east4 \
  --format='value(status.url)'
```

- [ ] Configure Stripe live webhook endpoint:

```text
<billing-backend-url>/billing/webhook
```

- [ ] Subscribe only required events:
  - `checkout.session.completed`
  - `invoice.paid`
  - `invoice.payment_failed`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`
  - `charge.dispute.created`
  - `charge.dispute.updated`
  - `charge.dispute.closed`
  - `charge.refunded`
  - `refund.created`
  - `refund.updated`
  - `refund.failed`

Extra events are acknowledged and ignored, but avoid subscribing to all events in production unless debugging.

## 4. Maintenance Mode Setup

The maintenance mode logic must be deployed before it can protect users. For first launch:

- [ ] Set production maintenance config before deployment:
  - `enabled: true`
  - `allowedEmails` includes:
    - `littlealan@gmail.com`
    - `alan.chan@sightsinger.app`
- [ ] Do not validate non-allowed blocking until the new frontend/backend are deployed.

## 5. Deployment

Build and deploy in this order.

- [ ] Build GPU-enabled main backend image.

```bash
scripts/build_backend_prod.sh
```

- [ ] Build lightweight CPU-only billing backend image.

```bash
scripts/build_billing_backend_prod.sh
```

- [ ] Deploy GPU-enabled main app backend.

```bash
scripts/deploy_backend_prod.sh
```

- [ ] Deploy CPU-only billing backend.

```bash
scripts/deploy_billing_backend_prod.sh
```

- [ ] Deploy Firebase Functions billing scheduler.

```bash
npx -y firebase-tools@latest deploy \
  --project sightsinger-app \
  --only functions:billing
```

- [ ] Deploy app frontend / Firebase Hosting.

```bash
scripts/deploy_frontend_prod.sh
```

- [ ] Confirm Firebase Hosting rewrite for `/billing/**` points to `sightsinger-billing-api`.

## 6. Smoke Tests In Maintenance Mode

Run these before opening access to all users.

- [ ] Visit `https://sightsinger.app/` and confirm marketing pages load.
- [ ] Visit `https://app.sightsinger.app/app` as a non-allowed email and confirm maintenance page blocks app access.
- [ ] Sign in with an allowed email and confirm app access works.
- [ ] Confirm `/maintenance/status` returns allowed for allowed email.
- [ ] Confirm `/credits` loads for allowed email.
- [ ] Confirm `/api/voicebanks` lists only enabled commercial-use voicebanks.

## 7. Live Stripe Checkout Test

Use an allowed email while maintenance mode is still enabled.

- [ ] Start checkout from the app UI.
- [ ] Complete a live Stripe payment with the intended live test card/payment method.
- [ ] Confirm checkout redirects back to the app.
- [ ] Confirm Firestore user billing state:
  - `billing.activePlanKey` matches selected plan.
  - `billing.stripeSubscriptionStatus` is active or equivalent.
  - `billing.latestInvoiceStatus=paid`.
  - `credits.balance` is topped up to the plan monthly allowance.
  - `credits.lastGrantType=grant_paid_subscription_cycle`.
- [ ] Confirm Stripe webhook event audit document exists for:
  - `checkout.session.completed`
  - `invoice.paid`
- [ ] Confirm duplicate event handling is idempotent if Stripe retries an event.
- [ ] Open Billing Portal from the app and confirm the portal opens.

## 8. Cancellation And Portal Tests

- [ ] In Stripe Customer Portal, cancel at period end.
- [ ] Confirm app shows cancellation-at-period-end state.
- [ ] Confirm entitlement remains paid until period end.
- [ ] Manually trigger or simulate immediate cancellation for a test account.
- [ ] Confirm `customer.subscription.deleted` is processed.
- [ ] Confirm billing state reverts to free.
- [ ] Decide and verify expected credit behavior after immediate cancellation:
  - Current behavior preserves already granted credits.
  - If product policy requires reducing credits immediately, update implementation before launch.

## 9. Scheduled Credit Refresh Test

- [ ] Create or select a test user whose `billing.nextCreditRefreshAt` is due.
- [ ] Run or trigger `refreshCredits`.
- [ ] Confirm due user refresh is processed.
- [ ] Confirm `billing.lastCreditRefreshAt` and `billing.nextCreditRefreshAt` advance correctly.
- [ ] Confirm `credits.balance` and `credits.monthlyAllowance` match plan.
- [ ] Confirm metrics/logging for refresh run are emitted.

## 10. Final Pre-Open Checks

- [ ] Confirm production logs for `sightsinger-api` show no startup errors.
- [ ] Confirm production logs for `sightsinger-billing-api` show no startup errors.
- [ ] Confirm `sightsinger-billing-api` has no GPU attached and `min-instances=0`.
- [ ] Confirm Stripe webhook delivery has no failing retries.
- [ ] Confirm live Stripe customer, subscription, invoice, and portal flows are correct.
- [ ] Confirm maintenance block still protects non-allowed users.
- [ ] Confirm allowed users can synthesize audio using enabled commercial-use voices.

## 11. Open Launch

- [ ] Disable production maintenance mode:
  - `enabled: false`
- [ ] Confirm non-allowed regular account can access the app.
- [ ] Confirm a free-plan user sees the permanent free plan state.
- [ ] Confirm paid checkout is visible and reachable.
- [ ] Monitor Cloud Run logs, Firebase logs, Stripe webhook delivery, and Firestore writes for at least 30 minutes.

## 12. Marketing Email

- [ ] Confirm Brevo campaign HTML uses current pricing and links.
- [ ] Confirm preview text is set.
- [ ] Confirm unsubscribe/footer settings are correct.
- [ ] Send a test email to internal addresses.
- [ ] Send launch email campaign.
- [ ] Monitor signups, checkout attempts, webhook delivery, and support inbox.

## 13. Rollback

If payment launch must be paused:

- [ ] Re-enable maintenance mode if app access must be blocked.
- [ ] Disable or hide checkout CTAs if only billing needs to pause.
- [ ] Disable Stripe webhook endpoint only if processing is unsafe.
- [ ] Keep existing subscriptions intact unless a Stripe-side rollback is explicitly required.
- [ ] Communicate any user-facing impact before sending further marketing email.
