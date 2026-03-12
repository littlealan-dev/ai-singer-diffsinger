# Linear-ready P0/P1 Tasks for `e13baf7`

## 1) Harden MXL extraction against decompression amplification
- **Title**: Harden MXL extraction against decompression amplification
- **Description**:
  The upload flow currently enforces a limit on compressed upload size, but then reads the referenced XML entry from `.mxl` archives without an explicit uncompressed-size ceiling. This creates a decompression-amplification risk that can cause memory/CPU pressure and elevated 5xx under load.

  Implement bounded extraction with a configurable uncompressed limit, fail fast with 413 on overflow, and add stage metrics for detection.
- **Severity**: P1
- **Labels**: security, reliability, backend, uploads
- **Acceptance Criteria**:
  - Introduce config `BACKEND_MAX_UNCOMPRESSED_XML_MB` (or equivalent).
  - `.mxl` extraction validates `ZipInfo.file_size` and enforces cumulative chunk limit.
  - Oversized uncompressed payload returns `HTTP 413` with deterministic error detail.
  - No unbounded `archive.read` for XML payload path.
  - Unit and API tests cover normal + oversized archive cases.
- **Suggested files touched**:
  - `src/backend/main.py`
  - `src/backend/config/__init__.py`
  - `env/dev.env`
  - `env/prod.env`
  - `tests/test_backend_api.py`

---

## 2) Normalize malformed `.mxl` failures to stable client 400s
- **Title**: Normalize malformed `.mxl` failures to stable client 400s
- **Description**:
  Corrupt/non-zip `.mxl` uploads can currently propagate low-level archive errors. These should be translated to deterministic client-visible validation failures, not server faults.

  Add explicit exception handling around archive open/decode and map malformed inputs to `HTTP 400` with stable error contracts.
- **Severity**: P1
- **Labels**: correctness, reliability, api-contract, backend
- **Acceptance Criteria**:
  - Corrupt/non-zip `.mxl` always returns `HTTP 400`.
  - Error body uses a stable detail/code for malformed archive input.
  - No 500s emitted for client-invalid `.mxl` payloads.
  - API tests cover malformed archive bytes and invalid container XML scenarios.
- **Suggested files touched**:
  - `src/backend/main.py`
  - `tests/test_backend_api.py`

---

## Optional implementation split (for planning)
### Option A (quick fix)
- Pre-check entry `file_size`, catch `zipfile.BadZipFile`, return 400/413 accordingly.

### Option B (proper fix)
- Stream extraction in chunks with cumulative guards, typed exceptions for archive validation, structured metrics (`upload_normalize_ms`, `upload_invalid_archive_count`, `upload_oversize_archive_count`) and alerting.
