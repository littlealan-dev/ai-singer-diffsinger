# Deployment Task List (Firebase + Cloud Run)

This checklist focuses on the remaining steps to deploy the current codebase to Firebase Hosting and Cloud Run.
It assumes you already created the Firebase project and enabled Auth/Firestore/Storage.

## 1) Cloud Project & Billing
- Done.

## 2) Firebase Console Setup
- Firebase Auth: Done.
- App Check: Done.
- Firestore:
  - Switch from dev rules to production rules when ready.
- Storage:
  - Ensure bucket exists and region matches Cloud Run.
  - Grant the Cloud Run service account access.

## 3) Secrets (Gemini)
- Done.

## 4) Frontend Config
- Done.

## 5) Backend Container Build
- Build and push API image (example):
  - `gcloud builds submit --tag gcr.io/sightsinger-app/ai-singer-api`
- Ensure requirements include `google-cloud-secret-manager` and `google-cloud-storage`.

## 6) Cloud Run Deploy
- Deploy the API service:
  - `gcloud run deploy api --image gcr.io/sightsinger-app/ai-singer-api --region YOUR_REGION --allow-unauthenticated`
  - Set env vars:
    - `APP_ENV=prod`
    - `BACKEND_AUTH_DISABLED=false`
    - `BACKEND_USE_STORAGE=true`
    - `STORAGE_BUCKET=sightsinger-app.appspot.com`
    - `LLM_PROVIDER=gemini`
  - Inject Secret Manager:
    - `--set-secrets GEMINI_API_KEY=projects/sightsinger-app/secrets/GEMINI_API_KEY:latest`

## 7) Firebase Hosting Deploy
- `firebase deploy --only hosting`

## 8) Security Rules
- Update Firestore rules to user‑scoped rules (no open dev rules).
- Update Storage rules (already set to `sessions/{uid}/...`):
  - `firebase deploy --only firestore,storage`

## 9) App Check Enforcement
- In Firebase Console, switch App Check to **Enforce** for the web app.
- Ensure `APP_ENV=prod` (App Check required server‑side).

## 10) Post‑Deploy Verification
- Create a session from the UI.
- Upload a MusicXML file.
- Ensure a job document is created in Firestore.
- Ensure Storage has `sessions/{uid}/{sessionId}/jobs/{jobId}/output.mp3`.
- Play audio and confirm no 401/403 errors.
