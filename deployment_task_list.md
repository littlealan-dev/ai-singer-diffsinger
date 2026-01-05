# Deployment Task List (Firebase + Cloud Run)

This checklist focuses on the remaining steps to deploy the current codebase to Firebase Hosting and Cloud Run.
It assumes you already created the Firebase project and enabled Auth/Firestore/Storage.

## 1) Cloud Project & Billing
- Confirm billing is enabled for `sightsinger-app`.
- Set the active project:
  - `gcloud config set project sightsinger-app`

## 2) Firebase Console Setup
- Firebase Auth:
  - Verify desired providers are enabled (Google + Email/Password already done).
- Firestore:
  - Switch from dev rules to production rules when ready.
- Storage:
  - Ensure bucket exists and region matches Cloud Run.
- App Check:
  - Register Web app and select reCAPTCHA v3.
  - Keep the site key for `VITE_FIREBASE_APP_CHECK_KEY`.

## 3) Secrets (Gemini)
- Create Gemini API key in Google AI Studio.
- Store it in Secret Manager:
  - `gcloud services enable secretmanager.googleapis.com --project sightsinger-app`
  - `gcloud secrets create GEMINI_API_KEY --replication-policy="automatic" --project sightsinger-app`
  - `printf "YOUR_GEMINI_KEY" | gcloud secrets versions add GEMINI_API_KEY --data-file=- --project sightsinger-app`
- Grant the Cloud Run service account access:
  - `gcloud secrets add-iam-policy-binding GEMINI_API_KEY --member="serviceAccount:YOUR_RUN_SA@YOUR_PROJECT_ID.iam.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"`

## 4) Frontend Config
- Populate `ui/.env.local` with Firebase config and App Check key:
  - `VITE_FIREBASE_API_KEY=...`
  - `VITE_FIREBASE_AUTH_DOMAIN=...`
  - `VITE_FIREBASE_PROJECT_ID=...`
  - `VITE_FIREBASE_STORAGE_BUCKET=...`
  - `VITE_FIREBASE_MESSAGING_SENDER_ID=...`
  - `VITE_FIREBASE_APP_ID=...`
  - `VITE_FIREBASE_APP_CHECK_KEY=...`
- Install UI deps and build:
  - `cd ui && npm install && npm run build`

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

