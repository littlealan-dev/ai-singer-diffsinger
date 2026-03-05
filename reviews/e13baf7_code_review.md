## A. Executive Summary
- **Decision: Ship-with-followups** (no hard blocker observed, but two P1 reliability/security issues should be fixed before broad rollout).
- Top risk #1 (**P1, Security/Reliability**): MXL decompression is unbounded (`ZipFile.read` full entry), so compressed payload can bypass upload-size controls and consume excessive memory/CPU/disk.
- Top risk #2 (**P1, Correctness/Reliability**): malformed `.mxl` (`zipfile.BadZipFile`) is not translated to a 4xx and can bubble as 500.
- Top risk #3 (**P2, Performance/Cost**): MXL extraction + `write_text` are synchronous in async request path, increasing event-loop blocking under concurrent uploads.
- Fix sequence:
  1) Add bounded/streamed MXL extraction with explicit uncompressed-size limits and fail-fast.
  2) Normalize MXL parse errors to client errors (400) with actionable details.
  3) Move extraction/writes to `asyncio.to_thread` and add latency/error metrics for upload parse stages.

## B. Risk Register
| ID | Severity | Category | Likelihood | Blast Radius | Detectability | Location | Short Title | Recommendation |
|---|---|---|---|---|---|---|---|---|
| R1 | P1 | Security/Reliability | Med | Service | Med | `src/backend/main.py` upload + `_read_musicxml_content` | Unbounded MXL decompression | Enforce max uncompressed bytes and stream extraction; reject oversize archive entries. |
| R2 | P1 | Correctness/Reliability | Med | Service | Easy | `src/backend/main.py` upload path | Invalid MXL returns 500 | Catch `zipfile.BadZipFile`/decode errors and return HTTP 400 with stable error code. |
| R3 | P2 | Performance | Med | Service | Hard | `src/backend/main.py` async endpoint | Blocking file I/O in async route | Offload extraction/write to threadpool and add timers for upload stages. |
| R4 | P2 | Cost/Latency | Med | Service | Med | `env/prod.env` | Forced high LLM thinking level in prod | Gate by feature flag or workload type; monitor p95 latency and token spend before default-on. |

## C. Findings
### [P1] MXL decompression bypasses upload-size guard
- Category: Security / Reliability
- Evidence: upload size is enforced only while writing the compressed file (`_write_upload`), then `.mxl` is fully decompressed via `archive.read(xml_name)` with no uncompressed-size bound.
- Why it matters: A small compressed archive can inflate massively, causing memory/disk pressure and request amplification; operationally this can trigger OOMs or worker starvation.
- Fix:
  1. Add `BACKEND_MAX_UNCOMPRESSED_XML_MB` config.
  2. In `_read_musicxml_content`, inspect `ZipInfo.file_size` before reading.
  3. Stream read in chunks and stop when cumulative bytes exceed limit.
  4. Return `HTTPException(413)` for over-limit payloads.
- Tests:
  - Add test for a zip entry whose uncompressed size exceeds configured max.
  - Add fuzz-ish test with multiple XML entries and ensure only referenced rootfile is processed.

### [P1] Corrupt `.mxl` maps to 500 instead of 4xx
- Category: Correctness / Reliability / API-Contract
- Evidence: `upload_musicxml` calls `_read_musicxml_content` before try/except block; `_read_musicxml_content` opens zip directly and can raise `zipfile.BadZipFile`.
- Why it matters: Invalid client input should not page on-call as server errors; incorrect status codes degrade retry behavior and observability signal quality.
- Fix:
  1. Wrap MXL extraction in `try/except (zipfile.BadZipFile, UnicodeDecodeError, ValueError)`.
  2. Return `HTTPException(400, detail="Invalid MusicXML archive")`.
  3. Emit structured warning log with session_id and filename (no payload).
- Tests:
  - Add unit/API test uploading random bytes with `.mxl` extension and assert 400.
  - Validate error body contract remains stable.

### [P2] Event-loop blocking in upload path for MXL canonicalization
- Category: Performance / Scalability
- Evidence: `.mxl` extraction + `Path.write_text` run inline in async handler.
- Why it matters: Under concurrent uploads, synchronous disk/zip operations can increase tail latency and reduce throughput.
- Fix:
  1. Move extraction + write into helper invoked via `await asyncio.to_thread(...)`.
  2. Prefer binary writes with explicit bytes to avoid duplicate encode/decode path.
- Tests:
  - Add benchmark/integration test with parallel uploads and assert no significant event-loop lag regression.

### [P2] Production latency/cost risk from default high thinking level
- Category: Performance / Cost / Reliability
- Evidence: `GEMINI_THINKING_LEVEL=high` set in production env defaults.
- Why it matters: Higher reasoning settings can materially increase latency and token usage; this may impact SLO and spend during peak traffic.
- Fix:
  1. Keep default blank/standard in prod and enable high thinking via controlled flag/segment.
  2. Add metric dimensions (`thinking_level`) on LLM calls and alert on p95/token budget.
- Tests:
  - Add config test ensuring env override precedence.
  - Add runtime metric assertion in integration tests (if metrics sink is mocked).

## D. Architectural Review
- Current architecture (inferred): FastAPI backend, async orchestration, filesystem/Firestore session store abstraction, tool-router for parsing and synthesis, LLM-driven planning loop.
- What is good:
  - Canonical `score.xml` extraction for `.mxl` fixes downstream parser/path consistency.
  - Config-driven toggle (`INJECT_FULL_PARSED_SCORE_JSON`) limits prompt bloat risk.
  - Regression test added for zipped MusicXML happy-path.
- What is risky:
  - Upload endpoint mixes async request handling with sync archive/file processing.
  - Input validation controls are split across stages and currently not defense-in-depth for compressed content.
  - Error mapping for client-invalid files is incomplete.
- Suggested target architecture (minimal change):
  - Isolate upload pipeline into stages (`save_upload` → `normalize_score_source` → `parse_score`) with explicit typed errors and per-stage metrics.
- Migration strategy:
  1) Introduce `normalize_musicxml_upload(path, limits)` helper with bounded extraction.
  2) Wire helper through threadpool in upload route.
  3) Add API-contract tests for invalid/oversized archives.
  4) Roll out with metrics/alerts on 4xx/5xx split and upload latency percentiles.

## E. Production Readiness Checklist
- Config & secrets: **Pass (partial)** — new config added and defaulted safely, but prod thinking-level default needs validation.
- Deploy/rollback plan: **Fail** — no explicit rollout/rollback guidance captured for changed upload semantics.
- DB migrations safety: **Pass** — no schema migration in this diff.
- Feature flags / gradual rollout: **Fail** — no canary flag for high-thinking default in prod.
- Rate limiting / backpressure: **Fail** — no evidence of per-endpoint upload concurrency/backpressure controls.
- Timeouts / retries / circuit breakers: **Pass (partial)** — existing LLM timeout config present; upload path still lacks bounded decompression timeout semantics.
- Logging / metrics / tracing: **Fail (partial)** — no new metrics/logs for extraction failures/latency stages.
- Alerting signals + runbooks: **Fail** — no updates tied to new failure modes.
- Capacity assumptions: **Fail** — no documented decompression amplification assumptions.
- Dependency versioning & licensing: **Pass** — no dependency delta in this diff.

## F. Test Plan Gaps
- Missing unit tests:
  - `_read_musicxml_content` with malformed zip and oversized XML entry.
- Missing integration tests:
  - Upload `.mxl` with corrupt archive and assert deterministic 400 contract.
  - Upload compressed bomb-like payload to verify 413 and no excessive resource usage.
- Missing e2e tests:
  - Parallel upload scenario with mixed `.xml`/`.mxl` to validate throughput/latency.
- Load/perf tests:
  - Required for upload endpoint after adding canonicalization path.
- Failure-mode tests:
  - Partial write failure cleanup of canonical `score.xml` file.
  - Parser failure after canonical extraction (ensure metadata/session consistency).

## G. Diff Comments (PR-style)
- `src/backend/main.py:186-191` — MXL extraction performs full `archive.read` without uncompressed-size limits — Add a bounded streaming extractor using `ZipInfo.file_size` and chunked reads.
- `src/backend/main.py:186-191` — Sync decompression/write inside async handler can block the event loop — Wrap normalization in `await asyncio.to_thread(...)`.
- `src/backend/main.py:186-191` — `zipfile.BadZipFile` is uncaught and surfaces as 500 — Catch and map to `HTTPException(400, "Invalid MusicXML archive")`.
- `tests/test_backend_api.py:1562-1598` — Happy-path test exists for zipped upload but no malformed archive coverage — Add negative-path API test for invalid `.mxl` bytes.
- `env/prod.env:37` — Setting `GEMINI_THINKING_LEVEL=high` globally may increase p95/token spend — Gate via environment tier/feature flag and verify with telemetry.

## Scoring (out of 10)
- Correctness: **7/10** — Canonical `.mxl` handling is improved, but invalid archive error mapping is incomplete.
- Reliability: **6/10** — Unbounded decompression and blocking I/O in async path create incident potential.
- Security: **6/10** — Archive inflation vector is not mitigated by current upload-size check.
- Performance: **6/10** — Potential event-loop blocking and increased LLM thinking in prod default.
- Maintainability: **8/10** — Changes are localized and config-driven; tests improved for happy path.
- Observability: **6/10** — Missing metrics and explicit logs for new normalization path/failure modes.
- **Confidence: Medium** (review based on repository state + latest commit diff; no prod telemetry available).

## Action Tasks for P0/P1 (Linear-ready)
### 1) Harden MXL extraction against decompression amplification
- Owner suggestion: SE
- Effort: M
- Acceptance Criteria:
  - Upload rejects `.mxl` when uncompressed XML exceeds configured max with HTTP 413.
  - Extraction is chunked/streamed; no full unbounded `archive.read` for XML payload.
  - Tests cover oversized uncompressed archive entry.
- Option A (minimal): Add `ZipInfo.file_size` gate before `archive.read`.
- Option B (proper): Stream decompress with chunk limit + per-stage metrics and timeout budget.

### 2) Normalize invalid MXL errors to client-safe 400 contract
- Owner suggestion: SE + QA
- Effort: S
- Acceptance Criteria:
  - Corrupt/non-zip `.mxl` upload returns HTTP 400 with stable error detail.
  - No 500 emitted for client-invalid `.mxl` payloads.
  - Negative-path tests added and passing.
- Option A (minimal): Catch `BadZipFile` in upload route and map to `HTTPException(400)`.
- Option B (proper): Introduce typed normalization errors and centralized API exception mapping.
