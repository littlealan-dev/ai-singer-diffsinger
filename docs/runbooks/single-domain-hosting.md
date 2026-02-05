# Single-Domain Hosting Plan (sightsinger.app)

Goal: Serve the marketing site (SSR Next.js) and the SPA (app/demo) under a single domain `sightsinger.app` to avoid cross-domain auth/storage issues.

## Target Routing
- `https://sightsinger.app/` and marketing pages (SSR): served by Next.js (App Hosting / Cloud Run)
- `https://sightsinger.app/app` and `https://sightsinger.app/demo`: served by SPA static build (Firebase Hosting)

## Overview
Use **Firebase Hosting** as the single front door for `sightsinger.app` and route:
- marketing routes -> SSR backend (App Hosting / Cloud Run)
- `/app` and `/demo` -> SPA static files

## Step 0: Prep
- Confirm you want a single domain: `sightsinger.app`
- Pick one Firebase Hosting site as the front door (current project: `sightsinger-app`)

## Step 1: SPA build output under Hosting
1. Build SPA to `ui/dist`.
2. Configure Hosting to serve SPA under `/app` and `/demo`.
   - Use Hosting rewrites to map:
     - `/app/**` -> `/app/index.html`
     - `/demo/**` -> `/demo/index.html`
   - Or, if you keep a single SPA build, use rewrites to `/index.html` and mount it under `/app` + `/demo`.

## Step 2: Marketing SSR behind Hosting
1. Keep Next.js marketing deployed on App Hosting (or Cloud Run).
2. Create Hosting rewrite for all other routes to the SSR backend.
   - Example: all paths except `/app/**` and `/demo/**` rewrite to App Hosting backend.

## Step 3: Firebase Hosting config (firebase.json)
Update `hosting` to:
- Serve static `ui/dist` at root
- Add rewrites:
  - Preserve Firebase Hosting reserved paths first:
    - `/__/auth/**` -> `/__/auth/handler`
    - `/__/firebase/**` -> `/__/firebase/init.json`
  - `/app/**` -> `/index.html`
  - `/demo/**` -> `/index.html`
  - (Catch-all) -> App Hosting backend for marketing via Cloud Run rewrite:
    - `run.serviceId = sightsinger-marketing-be`
    - `run.region = us-east4`

## Step 4: Auth domains
Update Firebase Auth authorized domains:
- `sightsinger.app`
- `www.sightsinger.app` (if used)

## Step 5: Update env/config
- Set SPA base URLs to `https://sightsinger.app`
- Update magic-link continue URL to use `https://sightsinger.app/app?finishSignIn=true`
- Ensure CORS allowlist includes `https://sightsinger.app`

## Step 6: DNS
- Point `sightsinger.app` and `www.sightsinger.app` to Firebase Hosting
- App subdomain `app.sightsinger.app` optional (can remove once stable)

## Step 7: Verification
- `https://sightsinger.app/` loads marketing SSR
- `https://sightsinger.app/what-it-does` works
- `https://sightsinger.app/app` loads SPA
- `https://sightsinger.app/demo` loads SPA
- Auth magic links complete without prompt

## Step 8: Rollback
- Restore previous DNS
- Remove Hosting rewrites
- Re-enable `app.sightsinger.app`
