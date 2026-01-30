# SightSinger.app – Deployment Architecture & Change List

This document describes the **target deployment architecture** and the **exact implementation tasks** for migrating SightSinger.app to an SEO-friendly, production-ready setup using Firebase App Hosting, Firebase Hosting, and Cloud Run.

Audience: coding agent / infra automation agent

---

## 1. Final Architecture (Approved)

### Domains & Responsibilities

| Domain | Platform | Purpose |
|------|---------|---------|
| `sightsinger.app` | Firebase **App Hosting** (Next.js) | Marketing site, SEO pages, blog/docs |
| `app.sightsinger.app` | Firebase **Hosting** (React/Vite SPA) | Authenticated web app |
| `api.sightsinger.app` | **Cloud Run** (Python, containerized) | Audio rendering, AI jobs, backend APIs |
| `www.sightsinger.app` | Firebase **App Hosting** (Next.js) | Redirect → sightsinger.app |

---

## 2. High-Level Request Flow

User
- visits sightsinger.app  
  - App Hosting (Next.js SSR/SSG)  
    - Marketing pages  
    - SEO / OG / JSON-LD  
    - CTA → app.sightsinger.app  

- visits app.sightsinger.app  
  - Firebase Hosting (SPA)  
    - Login (Google redirect / magic link)  
    - /app/* routes  
    - Calls API  

- API requests  
  - api.sightsinger.app (Cloud Run Python)

---

## 3. Routing Map

### sightsinger.app (App Hosting – Next.js)
- `/` → Marketing homepage (SSR/SSG)
- `/what-it-does`
- `/who-for`
- `/pricing`
- `/faq`
- `/features/*` (future)
- `/blog/*` (optional, recommended)
- `/docs/*` (optional)

CTAs:
- **Open app** → https://app.sightsinger.app/app
- **Sign in** → https://app.sightsinger.app/app?returnUrl=<encoded current page>

---

### app.sightsinger.app (Firebase Hosting – SPA)
- `/app/*` → main authenticated app
 - `/app?finishSignIn=true` → complete email magic link sign-in
 - Google redirect: use getRedirectResult on app load

Notes:
- No SEO needed
- Optionally inject `<meta name="robots" content="noindex, nofollow">` on `/app/*`
 - Auth flow stays on `/app` (no new login routes)

---

### api.sightsinger.app (Cloud Run – Python)
- `/api/*` (or root, depending on service)
- Protected by Firebase Auth ID tokens
- CORS allows:
  - https://sightsinger.app
  - https://app.sightsinger.app

---

## 4. Why This Architecture

- Next.js + App Hosting gives real SSR/SSG for SEO, OG cards, and AI search.
- SPA stays simple and cheap on Firebase Hosting.
- No path-based routing gymnastics under one apex domain.
- Firebase Auth still feels like SSO across subdomains.
- Future-proof for public share pages and content growth.

---

## 5. Implementation Change List

### A. Firebase Auth (Console + Code)

1. Firebase Console → Authentication → Settings → Authorized domains
   - Add sightsinger.app
   - Add www.sightsinger.app
   - Add app.sightsinger.app

2. Both frontends must use:
   - Same Firebase project
   - Same auth providers

3. Auth UX contract:
   - Both sites may initiate auth
   - All auth flows complete on app.sightsinger.app `/app?finishSignIn=true`

---

### B. Marketing Site (Next.js on Firebase App Hosting)

Goal: SEO-first public site on sightsinger.app

Tasks:
1. Create a new Next.js app (App Router) at `marketing/`.
2. Deploy via Firebase App Hosting (repo-connected).
3. Implement pages:
   - /
   - /what-it-does
   - /who-for
   - /pricing
   - /faq
   - optional /blog/*, /docs/*
4. For each page:
   - title + meta description
   - canonical URL
   - OpenGraph + Twitter meta
   - JSON-LD: Organization, SoftwareApplication, FAQPage (where relevant)
5. Add CTA buttons:
   - Open app → https://app.sightsinger.app/app
   - Sign in → app subdomain `/app?returnUrl=...`

---

### C. SPA (React/Vite on Firebase Hosting)

Routes:
- /app/*
 - /app?finishSignIn=true

Google Redirect Sign-in:
- On /app: signInWithRedirect (if user chooses Google)
- After redirect: getRedirectResult on app load
- Redirect to returnUrl or /app

Email Magic Link:
- Store email in localStorage
- Send link to https://app.sightsinger.app/app?finishSignIn=true&returnUrl=...
- Complete sign-in on /app when finishSignIn=true
- Redirect to returnUrl or /app

---

### D. Backend (Cloud Run – Python)

- Update CORS allowlist for both domains
- Firebase ID token verification unchanged

---

### E. DNS & Domains

1. Map sightsinger.app (+ www) → Firebase App Hosting
2. Map app.sightsinger.app → Firebase Hosting
3. Map api.sightsinger.app → Cloud Run custom domain
4. Configure www → sightsinger.app redirect

---

## 6. Acceptance Checklist

- sightsinger.app pages render correct HTML meta
- OG previews work
- Sign-in redirect flow works
- Magic link flow works
- SPA loads authenticated state
- API calls succeed
- Authorized domains configured

---

## 7. Final Tech Decisions

- Marketing: Next.js on Firebase App Hosting
- App: React/Vite on Firebase Hosting
- Backend: Cloud Run (Python)
- Auth: Firebase Auth (redirect + magic link)
