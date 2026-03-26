# Session Score Storage Pointer LLD

## Goal

Move both session `originalScore` and `currentScore` payloads out of the Firestore session document and into object storage, while keeping only lightweight pointers and metadata in the session document.

This removes the current Firestore 1 MiB document-size failure mode for large parsed scores.

## Scope

Phase 1 covers:

- `sessions/{session_id}.originalScore`
- `sessions/{session_id}.currentScore`
- initial score upload
- later `currentScore` updates after preprocess / transform steps
- read paths that currently expect inline score dicts from the session snapshot

Out of scope for this change:

- chat history storage redesign
- preprocess plan history storage redesign
- score-summary redesign
- audio storage changes
- changing the raw uploaded MusicXML storage path

## Current Problem

Today, the Firestore-backed session document stores:

- `originalScore`
- `currentScore`
- `history`
- preprocess plan history
- metadata

For large scores, the session document can exceed Firestore's hard 1 MiB per-document limit.

The raw uploaded MusicXML file is already persisted to Cloud Storage, but the parsed score object is duplicated into Firestore.

## Design Summary

### New storage model

When `backend_use_storage = 1`:

- store parsed score JSON blobs in Cloud Storage
- keep only pointer metadata in session state / session documents

When `backend_use_storage = 0`:

- keep current behavior unchanged
- score dicts may remain in memory / local session state

This keeps no-storage mode simple while making local and production behavior consistent whenever storage mode is enabled.

## Storage Layout

Use dedicated score object paths under the existing `sessions/` prefix.

Proposed paths:

- `sessions/{user_id}/{session_id}/scores/original.json`
- `sessions/{user_id}/{session_id}/scores/current.v{version}.json`

Notes:

- `original.json` is overwritten only on new upload / reparse baseline reset
- `current.v{version}.json` is append-only by version
- the Firestore session document stores the active pointer and current version

## Firestore Session Document Changes

### Remove large inline fields from steady-state writes

Replace inline score payloads with pointer metadata:

- `originalScorePath: string | null`
- `originalScoreStorage: "gcs" | null`
- `currentScorePath: string | null`
- `currentScoreStorage: "gcs" | null`
- `currentScoreVersion: number`

Optional metadata:

- `originalScoreByteSize: number | null`
- `currentScoreByteSize: number | null`

### Backward compatibility

During rollout, read logic must continue to support legacy inline fields:

- `originalScore`
- `currentScore`

Read precedence:

1. if `originalScorePath` / `currentScorePath` exists, load from storage
2. else fall back to legacy inline `originalScore` / `currentScore`

Write behavior after rollout when storage mode is enabled:

- new writes use pointer fields only
- do not write new large inline score blobs

## Session Store API Changes

### Storage-driven behavior

Score blob persistence must be controlled by `backend_use_storage`, not by session-store backend type.

Rule:

- if `backend_use_storage = 1`, both session store implementations persist score blobs to object storage and keep pointers as the canonical session representation
- if `backend_use_storage = 0`, both session store implementations may keep inline score dicts as they do today

### FirestoreSessionStore

Change `FirestoreSessionStore` so it no longer treats score blobs as inline document fields when storage mode is enabled.

#### New helper methods

- `_original_score_storage_path(user_id: str, session_id: str) -> str`
- `_current_score_storage_path(user_id: str, session_id: str, version: int) -> str`
- `_serialize_score(score: Dict[str, Any]) -> bytes`
- `_load_score_from_storage(path: str) -> Dict[str, Any]`
- `_store_score_to_storage(path: str, score: Dict[str, Any]) -> int`
- `_reserve_next_score_version_transaction(session_id: str) -> int`

#### `set_original_score`

New behavior:

1. resolve `user_id` from the session document
2. build `original.json` storage path
3. serialize score as UTF-8 JSON
4. upload bytes to Cloud Storage
5. update Firestore document with:
   - `originalScorePath`
   - `originalScoreStorage = "gcs"`
   - optional size metadata
   - `lastActiveAt`
6. optionally clear legacy `originalScore`

#### `set_score`

New behavior:

1. reserve the next `currentScoreVersion` inside a Firestore transaction
2. build `current.v{version}.json` storage path from the committed version
3. serialize and upload score JSON to Cloud Storage
4. update Firestore document with:
   - `currentScorePath`
   - `currentScoreStorage = "gcs"`
   - `currentScoreVersion`
   - optional size metadata
   - `lastActiveAt`
5. optionally clear legacy `currentScore`

Transaction rule:

- version allocation must be atomic
- do not use a plain read-modify-write cycle for `currentScoreVersion`

Reason:

- concurrent score updates could otherwise allocate the same next version
- because the object path includes the version number, duplicate allocation could cause storage collisions or stale pointers

Two-step write rule:

- the transaction reserves the numeric version only
- the storage upload happens after reservation succeeds
- the pointer update happens only after upload succeeds

Failure behavior:

- if upload fails after version reservation, do not point `currentScorePath` at a missing object
- leaving a skipped version number is acceptable in Phase 1

#### `get_snapshot` / `_state_from_doc`

New behavior:

- if pointer fields exist, lazily load score JSON from storage and hydrate:
  - `SessionState.original_score`
  - `SessionState.current_score`
- if pointer fields do not exist, fall back to inline legacy fields

Result:

- downstream orchestrator and API code can keep reading `snapshot["original_score"]` and `snapshot["current_score"]["score"]`
- the session store owns the storage indirection

Caching rule:

- within one `get_snapshot()` call, if both state hydration and snapshot generation need the same score payload, load it once and reuse the in-memory dict
- Phase 1 does not require a cross-request cache
- an instance-local cache keyed by `storage_path` is optional, but it must not become a source of stale data

### SessionStore (filesystem-backed)

Change the filesystem-backed `SessionStore` to use the same score-storage behavior when storage mode is enabled.

Rules:

- if `backend_use_storage = 1`, `SessionStore` also writes `original_score` and `current_score` JSON blobs to object storage and keeps pointer metadata in session state
- if `backend_use_storage = 0`, `SessionStore` keeps the current inline in-memory behavior

Reason:

- local/dev should exercise the same parsed-score storage path as production when storage mode is enabled
- this improves pre-production fidelity and avoids environment-specific behavior drift

Implementation note:

- pointer resolution should stay behind the session-store API so orchestrator callers still receive hydrated score snapshots

## Serialization Format

Use plain JSON object storage.

Rules:

- UTF-8 encoding
- `application/json` content type
- preserve existing score dict structure exactly
- no gzip in Phase 1

Reason:

- simplest compatibility
- easy to inspect and debug in storage
- avoids introducing transparent compression handling into read paths

## Main API / Orchestrator Impact

### Upload path

Current upload flow in `main.py`:

1. parse uploaded MusicXML
2. set score summary
3. `set_original_score(session_id, score)`
4. `set_score(session_id, score)`

This sequence remains the same.

Only the underlying persistence implementation changes.

When storage mode is enabled:

- both session store implementations use object storage for parsed score blobs

When storage mode is disabled:

- both session store implementations keep the current inline score behavior

### Orchestrator

The orchestrator should not need semantic changes if the session snapshot remains hydrated with:

- `original_score`
- `current_score.score`

Important requirement:

- `FirestoreSessionStore.get_snapshot()` must still return the same snapshot shape expected by:
  - preprocess planning
  - preprocess baseline resolution
  - score APIs

## Reset and Cleanup

### `reset_for_new_upload`

Current behavior clears inline fields.

New storage-enabled behavior:

- clear pointer fields:
  - `originalScorePath`
  - `currentScorePath`
  - `currentScoreVersion`
- clear any remaining inline legacy score fields
- keep other existing reset behavior

Phase 1 cleanup policy for old storage objects:

- do not immediately delete prior score blobs
- rely on session/user lifecycle cleanup later

Reason:

- safer rollout
- avoids accidental data loss during migration

## Migration / Rollout

### Read compatibility

Must support all three states:

1. legacy session with inline score fields only
2. mixed session with pointer fields plus leftover inline fields
3. new session with pointer fields only

### Write compatibility

After deployment:

- if `backend_use_storage = 1`, new updates write pointer fields only
- if `backend_use_storage = 0`, current inline behavior remains
- old inline fields may remain on older session docs until naturally replaced/reset

### No bulk backfill in Phase 1

Do not migrate old sessions proactively.

Reason:

- sessions are ephemeral
- rollout complexity is not justified for temporary session state

## Error Handling

### Storage upload failure

If score upload to Cloud Storage fails:

- do not update Firestore pointer fields
- raise the existing request failure

This prevents dangling pointers.

### Storage read failure

If pointer exists but score blob cannot be loaded:

- raise explicit `ValueError` / `RuntimeError`
- log:
  - session id
  - score path
  - score kind (`original` or `current`)

Do not silently fall back to `None` for pointer-backed sessions.

### User ID assumption

Score storage path generation uses `user_id`.

Phase 1 assumption:

- storage-enabled sessions always have `userId`

Therefore:

- no legacy missing-`userId` fallback is needed in this design
- storage paths remain `sessions/{user_id}/{session_id}/...`

## Performance Notes

Tradeoff introduced:

- each score snapshot read may require Cloud Storage download

This is acceptable in Phase 1 because:

- the current production blocker is correctness, not latency
- score reads are much less frequent than chat token generation

Mitigation:

- keep pointer resolution inside the session store
- do not re-download the same score multiple times inside one request if already loaded in memory

Phase 2 optimization options:

- request-scope cache
- local temp-file cache
- gzip compression

## Security

Use the existing application bucket or storage emulator and the current service-account permissions.

Do not expose raw storage paths directly to the client.

Pointers remain internal server-side metadata only.

## Tests

### Unit tests

Add Firestore-session-store tests for:

- `set_original_score` uploads JSON and stores pointer fields
- `set_score` uploads JSON and increments versioned pointer path
- `get_snapshot` hydrates scores from pointer-backed storage
- `get_snapshot` still supports legacy inline score fields
- `reset_for_new_upload` clears pointer fields

Add filesystem-session-store tests for:

- storage-enabled mode uploads `original_score` / `current_score` JSON blobs
- storage-enabled mode hydrates scores from pointers
- storage-disabled mode preserves current inline behavior

### Integration tests

Add backend API tests for:

- upload of a normal score still returns the same `current_score` response shape
- preprocess planning still uses `original_score`
- derived score updates still advance `current_score.version`

### Regression test

Add a focused test for a large parsed score object with Firestore-backed sessions:

- verify that the session document write path no longer attempts to inline the full score blob

## Acceptance Criteria

- storage-enabled sessions no longer keep full `originalScore` and `currentScore` blobs inline in the canonical session representation
- session snapshot shape used by orchestrator and API remains unchanged
- large parsed scores no longer fail because the Firestore session document exceeds 1 MiB
- legacy sessions with inline score fields still work during rollout
- local/dev and production follow the same parsed-score storage behavior when `backend_use_storage = 1`
