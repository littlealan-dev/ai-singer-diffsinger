## A. Executive Summary (10 lines max)
- **Decision: Block** for production rollout until the two P1 security hardening items below are addressed.
- Scope reviewed: full feature branch (`feature/SIG-6`) across backend runtime paths, MCP bridge, orchestration flow, and frontend API client wiring.
- Previously flagged issues now fixed in this branch: bearer auth query fallback (`id_token`/`auth`), unbounded `.mxl` decompression, and waitlist transport failure handling.
- Top risk #1 (**P1 Security**): App Check token is still accepted from URL query and the frontend appends it to media URLs, enabling token leakage via logs/history/referrers.
- Top risk #2 (**P1 Security / Data Exposure**): `/sessions/{session_id}/score` reads `source_musicxml_path` from mutable score state without path-boundary enforcement.
- Additional risk (**P2 Observability**): credit-balance enrichment swallows all exceptions and silently degrades response quality.
- Additional risk (**P2 Operability**): very large generated artifacts are committed in `tests/output/`, significantly increasing branch diff size and review/CI friction.
- Notable strengths: strong progress toward production hardening in upload flow, playback token signing, and waitlist retry/error contract.

## B. Risk Register (table)
| ID | Severity | Category | Likelihood | Blast Radius | Detectability | Location | Short Title | Recommendation |
|---|---|---|---|---|---|---|---|---|
| RR-001 | P1 | Security | Med | Wide | Hard | `src/backend/main.py`, `ui/src/api.ts` | App Check token in query string | Remove query fallback and stop appending `app_check` to URLs; require header only. |
| RR-002 | P1 | Security / Data Exposure | Low-Med | Wide | Hard | `src/backend/main.py` | Unbounded trusted file path for `/score` | Enforce allowlisted path roots (session dir/project data dir) before reading score files. |
| RR-003 | P2 | Observability | Med | Service | Easy | `src/mcp/handlers.py` | Silent exception swallow in credit estimate | Log structured warning and return explicit degraded metadata. |
| RR-004 | P2 | Operability | High | Repo/CI | Easy | `tests/output/voice_parts_e2e/*` | Large generated artifacts tracked in Git | Move artifacts out of repo or into ignored fixture cache; keep minimal golden files only. |

## C. Findings (grouped by category)
### [P1] App Check token is transported in query parameters
- Category: Security
- Evidence:
  - Backend accepts query fallback: `src/backend/main.py:566`.
  - Frontend appends `app_check` into URLs: `ui/src/api.ts:94-100`, `ui/src/api.ts:194`, `ui/src/api.ts:216`.
- Why it matters: query tokens are commonly exposed through access logs, browser history, and referrer headers.
- Fix:
  1. Remove `request.query_params.get("app_check")` fallback in backend.
  2. Keep App Check strictly in `X-Firebase-AppCheck` header.
  3. Stop mutating `audio_url` with query params in frontend.
- Tests:
  - `GET /sessions/{id}/audio?...&app_check=...` without header -> 401.
  - Header-based App Check on audio/progress endpoints remains functional.

### [P1] `/score` endpoint trusts mutable `source_musicxml_path` without boundary checks
- Category: Security / Data Exposure
- Evidence:
  - `/score` prefers path from current score payload: `src/backend/main.py:435`, `src/backend/main.py:444-447`.
  - Path resolver returns unvalidated path directly: `src/backend/main.py:756-771`.
- Why it matters: if session score state is ever corrupted or becomes user-influenced, the endpoint can read arbitrary readable files.
- Fix:
  1. Require resolved score path to stay within allowlisted roots (`sessions_dir` and/or `data_dir`).
  2. Reject absolute paths outside allowlist with 400/403.
  3. Consider storing immutable backend-owned score artifact IDs instead of raw file paths in score payload.
- Tests:
  - Inject out-of-root path in current score state -> endpoint rejects.
  - In-root derived score path remains readable.

### [P2] Silent exception swallow in credit-estimate enrichment
- Category: Observability / Reliability
- Evidence: broad catch with `pass` in `src/mcp/handlers.py:251-253`.
- Why it matters: credit-service failures are hidden, making incident diagnosis and partial-outage handling harder.
- Fix:
  1. Log warning with correlation fields (`uid`, estimated credits, error class).
  2. Return explicit `balance_unavailable=true` (or equivalent) to callers.
- Tests:
  - Force `get_or_create_credits` failure and assert warning + degraded flag.

### [P2] Branch includes massive generated binary/json artifacts
- Category: Operability / Repo Hygiene
- Evidence: large additions under `tests/output/voice_parts_e2e/*` and `tests/plans/*` in branch diff.
- Why it matters: materially slows review tooling, branch sync, and CI checkout/storage.
- Fix:
  1. Keep only minimal deterministic fixtures under source control.
  2. Move bulky outputs to external artifact storage or regenerate-on-demand scripts.

## D. Architectural Review
- What improved significantly:
  - Upload flow now normalizes `.mxl` safely with bounded extraction (`src/musicxml/io.py`) and stable 400/413 mapping.
  - Waitlist integration now includes retry/error contracts and surfaces dependency failure as 503.
  - Bearer auth query fallback was removed; playback URLs are now signed with scoped tokens.
- Remaining systemic risk:
  - Security boundary inconsistency in transport (header vs query) and file path trust model.

## E. Production Readiness Checklist (pass/fail + notes)
- Authn/Authz boundary: **Fail (partial)** — bearer query fallback fixed, but App Check query fallback remains.
- Input/file handling: **Fail (partial)** — MXL bounds are fixed; score file path trust still lacks root enforcement.
- External dependency resilience: **Pass (partial)** — waitlist now retries and returns deterministic failure contracts.
- Observability: **Fail (partial)** — important exception path still silently swallowed.
- Deployability/operability: **Fail (partial)** — branch currently carries very large generated artifacts.

## F. Test Plan Gaps
- Add backend tests for App Check header-only enforcement on audio URL path.
- Add backend tests to reject out-of-root `source_musicxml_path` values in `/score`.
- Add handler test asserting degraded metadata/log behavior when credit lookup fails.

## G. Diff Comments (PR-style)
- `src/backend/main.py:566` — App Check query fallback keeps token in URL-space; switch to header-only verification.
- `ui/src/api.ts:94-100` — Client appends `app_check` to URL query; this leaks attestation tokens to logs/history.
- `src/backend/main.py:756-771` — Score path resolver trusts payload path without root checks; enforce session/data-root boundary.
- `src/mcp/handlers.py:251-253` — `except Exception: pass` hides credit lookup outages; log and return explicit degraded flag.

## Scoring (required)
- Correctness: **8/10**
- Reliability: **8/10**
- Security: **6/10**
- Performance: **7/10**
- Maintainability: **7/10**
- Observability: **6/10**
- **Confidence: Medium** (full branch code inspection; runtime tests not executed in this environment).

## Action Orientation for P0/P1 (required)
### Task 1 (P1)
- **Title**: Remove App Check query-token transport and enforce header-only
- **Owner suggestion**: Backend + Frontend
- **Effort**: S
- **Acceptance Criteria**:
  - Backend rejects query-only `app_check`.
  - Frontend no longer appends `app_check` to URLs.
  - Audio/progress flows continue to work with header transport.

### Task 2 (P1)
- **Title**: Enforce score-path allowlist before file reads in `/score`
- **Owner suggestion**: Backend
- **Effort**: S-M
- **Acceptance Criteria**:
  - `/score` rejects out-of-root file paths.
  - Existing in-session derived/original score retrieval remains functional.
  - Regression tests cover both allowed and blocked paths.

## Linear-ready list for P0/P1
- Title
  - Enforce header-only App Check transport (remove URL query path)
- Description
  - App Check token is currently accepted from query params and appended by frontend to media URLs. This leaks attestation tokens into URL-visible surfaces.
- Severity (P0/P1)
  - P1
- Labels (security/perf/reliability/etc)
  - security, backend, frontend, auth
- Acceptance Criteria (bullet points)
  - Query-only App Check requests are rejected.
  - Frontend sends App Check only via headers.
  - Media playback/progress polling remains functional.
- Suggested files touched
  - `src/backend/main.py`
  - `ui/src/api.ts`
  - `tests/test_backend_api.py`

- Title
  - Add path-boundary enforcement for `/sessions/{session_id}/score`
- Description
  - Current score retrieval trusts `source_musicxml_path` from session score payload without ensuring path is within approved roots.
- Severity (P0/P1)
  - P1
- Labels (security/perf/reliability/etc)
  - security, backend, file-io
- Acceptance Criteria (bullet points)
  - Out-of-root paths are rejected.
  - Valid session/data-root score paths are returned.
  - Tests cover both negative and positive cases.
- Suggested files touched
  - `src/backend/main.py`
  - `tests/test_backend_api.py`
