# Auth Marketing Email Opt-In LLD

## Goal

Add an optional marketing email checkbox to the app sign-in/sign-up modal while preserving double opt-in through Brevo and avoiding repeated confirmation emails for users whose backend opt-in state is already known.

The app should:

- Let users explicitly opt in during Google sign-in or magic-link sign-in.
- Start the Brevo double-opt-in flow only after authentication succeeds.
- Persist opt-in request state in the backend user record.
- Skip the backend opt-in request when the user record already has marketing opt-in state.
- Support a one-off admin migration for the small existing Brevo list.

## Non-Goals

- Do not replace Brevo as the source of truth for confirmed marketing list membership in V1.
- Do not block app sign-in or account creation on marketing opt-in.
- Do not implement Brevo webhook processing in V1.
- Do not pre-check the marketing checkbox.
- Do not call Brevo on every sign-in for lazy migration.

## Current State

The existing `Get Updates` flow sends a Brevo double-opt-in request from:

- App backend: `POST /waitlist/subscribe`
- Marketing site: `POST /api/waitlist/subscribe`

Both flows send consent attributes to Brevo. The authenticated app backend can now also write marketing opt-in state to the SightSinger user record when the submitted email matches the authenticated user.

## UX

Add an unchecked checkbox to the auth modal:

> Send me product updates and SightSinger news.

The checkbox should appear for:

- Google sign-in/sign-up.
- Magic-link sign-in/sign-up.

When auth succeeds and the checkbox was checked, the app submits the opt-in request in the background. The UI does not need a blocking confirmation state. If the backend determines the user already has stored opt-in state, the frontend silently clears the pending intent.

## Frontend Flow

### Google Auth

1. User opens auth modal.
2. User optionally checks the marketing updates checkbox.
3. User clicks `Continue with Google`.
4. Frontend stores a short-lived pending auth marketing intent locally:

```json
{
  "marketingOptIn": true,
  "source": "auth_modal_google",
  "createdAt": 1770000000000
}
```

5. Google redirects out and back.
6. Firebase auth completes.
7. Frontend detects authenticated user.
8. Frontend observes `users/{uid}.marketing` from Firestore.
9. If pending intent exists, is checked, is not expired, and user state does not already show `marketing.emailOptInRequested === true`, call:

```http
POST /marketing/opt-in
Authorization: Bearer <Firebase ID token>
```

10. If user state already shows `marketing.emailOptInRequested === true`, skip `POST /marketing/opt-in` and clear pending intent.
11. Clear pending intent after the backend returns success or a non-retryable decision.

The frontend must not call Brevo directly. Brevo API keys remain backend-only.

### Magic Link

1. User enters email.
2. User optionally checks the marketing updates checkbox.
3. User clicks `Get magic link`.
4. Frontend stores a short-lived pending intent keyed by email:

```json
{
  "email": "user@example.com",
  "marketingOptIn": true,
  "source": "auth_modal_magic_link",
  "createdAt": 1770000000000
}
```

5. User clicks the magic link.
6. Firebase auth completes.
7. Frontend compares authenticated email to pending intent email.
8. Frontend observes `users/{uid}.marketing` from Firestore.
9. If the email matches, the intent is checked, the intent is not expired, and user state does not already show `marketing.emailOptInRequested === true`, call `POST /marketing/opt-in`.
10. If user state already shows `marketing.emailOptInRequested === true`, skip `POST /marketing/opt-in` and clear pending intent.
11. Clear pending intent after success or non-retryable decision.

### Local Pending Intent

Use local storage with a TTL because magic-link auth may complete in a new tab after several minutes.

Suggested TTL: 24 hours.

The local value is only pending intent. It is not the durable consent record.

For local development only, where marketing and app may run on different localhost origins, the marketing app may pass a `marketingOptIn=1` URL parameter to the app auth callback. Production should rely on same-origin local storage.

## Post-Auth User State

After authentication completes, the frontend observes marketing opt-in state from the backend user record before deciding whether to call `POST /marketing/opt-in`.

Minimum fields needed by the frontend:

```ts
marketing?: {
  emailOptInRequested?: boolean
  emailOptInBrevoStatus?: string
  emailOptInRequestedAt?: Timestamp
}
```

Frontend behavior:

- If pending auth intent is absent, do nothing.
- If pending auth intent is expired, clear it.
- If pending auth intent is checked and `marketing.emailOptInRequested === true`, clear it and skip `POST /marketing/opt-in`.
- If pending auth intent is checked and `marketing.emailOptInRequested !== true`, call `POST /marketing/opt-in`.

Missing `marketing` or missing `marketing.emailOptInRequested` means `unknown`, not confirmed false.

## Backend API

### `POST /marketing/opt-in`

Authenticated endpoint.

Headers:

```http
Authorization: Bearer <Firebase ID token>
```

Request:

```json
{
  "source": "auth_modal_google",
  "consent_text": "Send me product updates and SightSinger news.",
  "pending_intent_created_at": "2026-05-17T00:00:00Z"
}
```

For magic link:

```json
{
  "source": "auth_modal_magic_link",
  "consent_text": "Send me product updates and SightSinger news.",
  "pending_intent_created_at": "2026-05-17T00:00:00Z"
}
```

The backend must derive the trusted `user_id` and `email` from the verified Firebase token, not from request body email.

Response:

```json
{
  "success": true,
  "status": "doi_requested",
  "requires_confirmation": true,
  "message": "Check your inbox to confirm your email subscription."
}
```

Possible statuses:

- `already_requested`
- `doi_requested`
- `dependency_unavailable`

## User Document Schema

Add these fields to the existing user record:

```ts
marketing: {
  emailOptInRequested: boolean
  emailOptInRequestedAt: Timestamp
  emailOptInSource: string
  emailOptInEmail: string
  emailOptInConsentText: string
  emailOptInBrevoStatus: "doi_requested" | "already_in_list"
}
```

Optional later fields if Brevo webhook/sync is added:

```ts
marketing: {
  emailOptInConfirmed: boolean
  emailOptInConfirmedAt: Timestamp
  emailUnsubscribedAt: Timestamp
}
```

## Backend Decision Flow

For `POST /marketing/opt-in`:

1. Verify Firebase ID token.
2. Read user ID and email from verified token.
3. Load user document.
4. If `marketing.emailOptInRequested === true`:
   - Return `already_requested`.
   - Do not call Brevo.
5. Call Brevo double-opt-in endpoint.
6. If Brevo DOI succeeds:
   - Write backend fields with `marketing.emailOptInBrevoStatus = "doi_requested"`.
   - Return `doi_requested`.
7. If Brevo DOI fails:
   - Do not write `marketing.emailOptInRequested = true`.
   - Return dependency failure.

## Existing Contact Migration

The existing Brevo marketing list is small enough to handle with a one-off migration rather than runtime lazy migration.

Suggested migration inputs:

- Export the Brevo contact list to CSV.
- Include at least `email`.
- Optional: include Brevo subscription metadata if available.

Migration behavior:

1. For each exported email, find matching `users/{uid}` where `email` equals the exported email.
2. If the user doc exists and does not already have `marketing.emailOptInRequested === true`, patch:

```ts
marketing.emailOptInRequested = true
marketing.emailOptInRequestedAt = migrationRunTimestamp
marketing.emailOptInSource = "brevo_export_migration"
marketing.emailOptInEmail = user.email
marketing.emailOptInConsentText = ""
marketing.emailOptInBrevoStatus = "already_in_list"
```

3. If no user doc exists, skip it. A future sign-in without checkbox should not trigger any Brevo lookup in V1.

The migration should be idempotent and should not overwrite newer user consent state.

## Get Updates Flow

The public `Get Updates` modal remains Brevo-first.

Authenticated app `Get Updates` submissions may also write backend user fields after Brevo DOI succeeds when the submitted email matches the authenticated user.

Marketing site `Get Updates` remains Brevo-only unless the user is already authenticated and same-origin auth state is available.

## Security

- Brevo API key is never exposed to the frontend.
- Backend derives user ID and email from Firebase token only.
- Backend ignores request body email for auth opt-in.
- Consent text is stored with the request state.
- The checkbox is opt-in only and must not be pre-checked.

## Test Cases

### New Google User, Checkbox Checked

- User signs in with Google.
- Pending intent exists.
- User doc has no marketing opt-in state.
- Backend calls Brevo DOI once.
- User doc stores `doi_requested`.

### New Google User, Checkbox Not Checked

- User signs in with Google.
- No pending intent exists.
- Frontend does not call `/marketing/opt-in`.
- Backend does not call Brevo.

### Existing User With Backend Opt-In State

- User signs in and checks checkbox again.
- Firestore user doc has `marketing.emailOptInRequested === true`.
- Frontend clears pending intent and skips `/marketing/opt-in`.
- Brevo is not called.

### Existing Brevo Contact Migrated By Script

- Migration script patches user doc with `emailOptInRequested === true`.
- User signs in and checks checkbox.
- Frontend skips `/marketing/opt-in`.
- Brevo is not called.

### Existing Brevo Contact Not Migrated

- User signs in without checking checkbox.
- Frontend does nothing.
- Backend does not call Brevo.
- User can still explicitly opt in later through the checkbox or Get Updates flow.

### Magic Link Email Mismatch

- Pending intent email does not match authenticated email.
- Frontend clears pending intent.
- Backend is not called.

### Brevo Failure

- Backend DOI call fails.
- Backend does not write opt-in-requested state.
- Frontend can retry on a later auth attempt if pending intent remains valid.

## Deferred

- Brevo webhook/sync for confirmed subscription and unsubscribe state.
- Admin migration script for Brevo export CSV.
- Centralized `/me` or `/user/bootstrap` endpoint.
