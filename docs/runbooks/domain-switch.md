# Domain Switch Runbook: /app and /demo to app.sightsinger.app

Goal: move app routes (`/app`, `/demo`) off the apex domain and onto `app.sightsinger.app`, while the marketing site lives on `sightsinger.app`.

## 1) Pre-flight
1. Confirm current production endpoints and ownership in Firebase:
   - App Hosting backend for marketing (Next.js)
   - Firebase Hosting site for SPA
   - Cloud Run custom domain for `api.sightsinger.app`
2. Lower Cloudflare DNS TTL for relevant records (e.g., 300s) at least 30 minutes before cutover.
3. Ensure you have access to:
   - Firebase Console
   - Cloudflare DNS + Redirects/Bulk Redirects

## 2) Firebase: map domains to the right backends
1. Firebase App Hosting (marketing):
   - Go to App Hosting > your backend (`sightsinger-marketing-be`).
   - Add custom domains:
     - `sightsinger.app`
     - `www.sightsinger.app`
   - Follow the DNS instructions that App Hosting gives you (do not guess record values).
2. Firebase Hosting (SPA):
   - Go to Hosting > your SPA site (the one deploying `ui/dist`).
   - Add custom domain `app.sightsinger.app`.
   - Follow the DNS instructions that Hosting gives you (do not guess record values).
3. Cloud Run (API):
   - No change if `api.sightsinger.app` remains as-is.

## 3) Cloudflare: DNS updates
Do this only after Firebase gives you the exact records to set.

1. Update/create the following DNS records in Cloudflare:
   - Apex marketing: `sightsinger.app` -> record values shown by Firebase App Hosting.
   - Marketing `www` -> record values shown by Firebase App Hosting.
   - App subdomain: `app.sightsinger.app` -> record values shown by Firebase Hosting.
2. Set proxy status to `DNS only` for all Firebase-managed records (Firebase TLS validation can fail behind Cloudflare proxy).
3. If Cloudflare shows pre-existing records for these names, remove or replace them exactly as Firebase specifies.

## 4) Redirects: send `/app` and `/demo` to the app subdomain
You need to redirect requests that hit the apex domain:
- `https://sightsinger.app/app` -> `https://app.sightsinger.app/app`
- `https://sightsinger.app/demo` -> `https://app.sightsinger.app/demo`

Choose one (only one) of these approaches:
1. Cloudflare Redirects:
   - Create two redirect rules:
     - `sightsinger.app/app*` -> `https://app.sightsinger.app/app` (preserve path/query if needed)
     - `sightsinger.app/demo*` -> `https://app.sightsinger.app/demo` (preserve path/query if needed)
   - Use 301 once verified, 302 during validation.
2. Marketing app (Next.js):
   - Add redirects at the Next.js layer so these paths never render marketing content.

## 5) Update application links and configs
Update references so everything points to the new app host and the marketing host stays apex.

1. SPA short-link and host logic (if still needed):
   - Review `ui/src/main.tsx` to ensure it does not redirect from `app.sightsinger.app` back to the apex.
2. Sitemap for SPA:
   - Update `ui/public/sitemap.xml` to `https://app.sightsinger.app` URLs only.
3. Auth and CORS allowlists:
   - Firebase Auth authorized domains should include:
     - `sightsinger.app`
     - `www.sightsinger.app`
     - `app.sightsinger.app`
   - Backend CORS allowlist should include both:
     - `https://sightsinger.app`
     - `https://app.sightsinger.app`
   - Check `env/prod.env` (`CORS_ALLOW_ORIGINS`) for accuracy.
4. Email and waitlist redirect URLs:
   - Ensure `BREVO_DOI_REDIRECT_URL` points to the marketing site path you expect (currently `sightsinger.app`).
5. Legal pages:
   - If legal routes are served by the marketing site, update any app links accordingly.

## 6) Verify cutover (manual checks)
1. Marketing:
   - `https://sightsinger.app/` renders the marketing home page.
   - `https://sightsinger.app/what-it-does` (and other marketing routes) render correctly.
2. App:
   - `https://app.sightsinger.app/app` loads the SPA.
   - `https://app.sightsinger.app/demo` loads the SPA demo route.
3. Redirects:
   - `https://sightsinger.app/app` redirects to `https://app.sightsinger.app/app`.
   - `https://sightsinger.app/demo` redirects to `https://app.sightsinger.app/demo`.
4. Auth:
   - Google redirect and email magic link complete on `app.sightsinger.app`.
5. API:
   - API calls succeed from both domains (check browser console for CORS errors).

## 7) Cleanup
1. Restore Cloudflare TTLs to normal values.
2. If you used temporary 302 redirects, switch them to 301 after verification.

## 8) Rollback plan
1. Revert Cloudflare DNS records to the previous state.
2. Disable or remove the `/app` and `/demo` redirects.
3. Remove any code/config changes made for the cutover.

## Related files to update in this repo
- `docs/specs/SightSinger_Deployment_Architecture.md`
- `ui/src/main.tsx`
- `ui/public/sitemap.xml`
- `env/prod.env`
- `ui/src/LegalTerms.tsx`
- `ui/src/LegalPrivacy.tsx`
