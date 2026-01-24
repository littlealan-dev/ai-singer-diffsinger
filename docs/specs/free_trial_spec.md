Free Trial Plan, UI Flow, and Design Spec
=========================================

Overview
--------
Enable a "Free Trial" that requires sign-in (Google or email magic link). Once authenticated, the user receives 10 credits, each credit covering 30 seconds of generated audio. Credits expire 14 days after grant. Also provide a "Join Waiting List" action for already signed-in users.

Assumptions and Definitions
---------------------------
- Credit: 1 credit = 30 seconds of generated audio time.
- Trial grant: 10 credits = 300 seconds total.
- Expiry: 14 days from the time the credits are granted.
- Usage rounding: charge per started 30-second block (ceil).
- Auth methods: Google OAuth or email magic link.
- "Join Waiting List": available only to signed-in users.
- "Free Trial" eligibility: first-time trial grant per user.

UI Flow
-------
Entry points:
- Landing page: "Start Free Trial" CTA.
- Pricing page: "Try Free Trial" CTA.
- Synthesis page: prompt to sign in if not authenticated.

Flow A: Start Free Trial (not signed in)
1) User clicks "Start Free Trial".
2) Auth modal opens with two options:
   - Continue with Google
   - Continue with Email (magic link)
3) User completes auth:
   - Google: OAuth consent, redirect back.
   - Email: enter email, receive link, click to confirm.
4) On first successful login, grant 10 credits with 14-day expiry.
5) Redirect to main app with a success toast:
   "Free trial activated: 10 credits (300s) available until <date>."
6) User can proceed to upload MusicXML and synthesize.

Flow B: Signed in, no credits or expired credits
1) User attempts synthesis.
2) If credits are 0 or expired:
   - Show "Free Trial Expired" panel with:
     - Remaining credits: 0
     - Expiry date (if exists)
     - Primary CTA: "Join Waiting List"
     - Secondary CTA: "Contact Us" or "Learn More"

Flow C: Signed in, credits available
1) User starts synthesis.
2) System estimates duration and required credits.
3) Confirmation UI:
   - "This will use N credits (M seconds). Proceed?"
4) On completion:
   - Show remaining credits and updated expiry in status panel.

Flow D: Join Waiting List (signed in)
1) User clicks "Join Waiting List".
2) Confirm modal:
   - Message: "We'll notify you about early access and updates."
   - Consent checkbox (optional): "Also send product updates."
3) User confirms.
4) Show success toast: "You are on the waiting list."

Design Spec
-----------
Global UI elements:
- Account badge with credit summary:
  - "Credits: 7 (210s) | Expires in 4d"
- Trial ribbon for new users:
  - "Free Trial: 10 credits, 14 days"

Auth modal:
- Title: "Sign in to start your free trial"
- Body copy: "Get 10 credits (300 seconds) valid for 14 days."
- Buttons:
  - "Continue with Google"
  - "Email me a magic link"
- Email field state:
  - Validated email input
  - CTA: "Send magic link"
  - Helper text: "We will email you a sign-in link."

Credit display:
- Placement: header and on synthesis confirmation dialog.
- States:
  - Active: show credits and expiry date.
  - Low (<=2 credits): highlight in amber.
  - Expired: show "Expired" badge and "Join Waiting List" CTA.

Synthesis confirmation:
- Before running:
  - Estimated duration (seconds)
  - Required credits (ceil(duration / 30))
  - Remaining credits after run
  - Primary CTA: "Generate"
  - Secondary CTA: "Cancel"

Errors and edge cases:
- Auth failure: show inline error, allow retry.
- Magic link expired: prompt to resend.
- Credit race (concurrent synthesis):
  - If credits change during confirmation, re-check and prompt user.
- Expired trial:
  - Block synthesis and show waiting list CTA.

Data and State Considerations
-----------------------------
Client states:
- isAuthenticated
- creditsRemaining
- creditsExpiryAt
- isTrialGranted
- isWaitlistJoined

Server-side expectations:
- Idempotent trial grant on first login.
- Credit deduction per synthesis request.
- Expiry enforcement at request time.
- Audit log of credit grants and usage.
- Waitlist entry per user (unique).

Implementation Plan
-------------------
Phase 1: Product and data model
1) Define trial credit policy and rounding rules.
2) Add user fields:
   - trial_credits_remaining
   - trial_credits_expiry_at
   - trial_granted_at
   - waitlist_joined_at
3) Add credit ledger for audit (optional but recommended).
4) Add waitlist collection with unique user ID.

Phase 2: Auth and trial grant
1) Implement Google OAuth and email magic link flows.
2) On first login, grant 10 credits and set expiry at now + 14 days.
3) Ensure idempotent grant logic.

Phase 3: Credit usage and enforcement
1) Compute required credits per synthesis request:
   - ceil(audio_seconds / 30)
2) Enforce expiry and remaining credits before synthesis starts.
3) Deduct credits after successful synthesis.
4) Return updated credit state to client.

Phase 4: UI integration
1) Add header credit summary component.
2) Add auth modal and "Start Free Trial" CTA.
3) Add synthesis confirmation dialog with credit usage.
4) Add expired/zero-credit panel with waiting list CTA.
5) Add "Join Waiting List" flow for signed-in users.

Phase 5: Analytics and monitoring
1) Track funnel events:
   - trial_cta_clicked
   - auth_completed
   - trial_granted
   - synthesis_started
   - credits_exhausted
   - waitlist_joined
2) Add basic dashboards for trial conversions and usage.

Open Questions
--------------
- Should credits be granted on first login or only after first synthesis?
- Should partial seconds be rounded up or allow prorated billing?
- Should the waiting list require explicit consent for marketing?
