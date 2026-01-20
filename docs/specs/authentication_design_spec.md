Authentication Design Spec
==========================

Scope
-----
Define the technical details for authentication, covering Google sign-in (redirect flow) and email magic link sign-in. This spec supports the high-level plan in `free_trial_implementation_plan.md`.

Core Decisions
--------------
- Google sign-in uses full-page redirect on all platforms for simplicity.
- Email sign-in uses Firebase magic links with an in-app completion step.
- Auth UI lives inside an in-app modal overlay, but provider consent screens are not embedded.

User Flows
----------
Google (redirect):
1) User clicks "Continue with Google" in Auth Modal.
2) App calls `signInWithRedirect` and navigates to Google.
3) On return, app calls `getRedirectResult` to finalize.
4) If first login, grant trial credits.

Email magic link:
1) User enters email and clicks "Send Magic Link".
2) App calls `sendSignInLinkToEmail` with `handleCodeInApp=true`.
3) App stores the email in `localStorage` for link completion.
4) User clicks the link and returns to `/app?finishSignIn=true`.
5) App calls `isSignInWithEmailLink` and `signInWithEmailLink`.
6) If first login, grant trial credits.

Technical Integration
---------------------
Frontend (`ui/src/firebase.ts`):
- `signInWithGoogleRedirect()` to initiate redirect flow.
- `completeGoogleRedirect()` to complete redirect on return.
- `sendMagicLink(email)` to send email sign-in link.
- `completeMagicLinkSignIn()` to finalize email sign-in.
- `onAuthChange()` to update UI state.
- `logOut()` to sign out and clear session.

Routing / Entry Points
----------------------
- `/app` is protected; unauthenticated users see Auth Modal.
- Query param `finishSignIn=true` triggers email link completion flow.

State and Storage
-----------------
- `localStorage.emailForSignIn` stores the email for magic link completion.
- On success, clear local storage and update auth state.
- Store auth user in app-level state via `useAuth`.

Error Handling
--------------
- Redirect failures: show error banner in the modal and allow retry.
- Invalid/expired magic links: show "Resend link" prompt.
- If email is missing at completion, prompt user to re-enter email.

Security Notes
--------------
- OAuth consent screens cannot be embedded in a div.
- Redirect avoids popup blockers and ensures consistent behavior.

Verification
------------
- Google redirect sign-in returns to `/app` and authenticates.
- Magic link works across tabs and returns to `/app` with valid session.
