# Instrumental MIDI Storage HLD

**Status:** Implemented
**Scope:** Make the instrumental MIDI files survive across Cloud Run instances,
the same way the active MusicXML and the rendered audio already do.

## 1. Problem

A take with instrument tracks produces two MIDI files: the written order and
the repeat order. Today they exist only on the disk of the instance that
rendered the take:

```
job (instance A)
    build MIDI → session_dir/instrumental-written-{signature}.mid
                 session_dir/instrumental-expanded-{signature}.mid
    session files (Firestore):   instrumental_midi_original_path = "data/sessions/{sid}/instrumental-written-{sig}.mid"
    job record (Firestore):      performanceMidiPaths = {"originalPath": "/app/data/sessions/...", ...}

GET /sessions/{sid}/instrumental-midi   (any instance)
    signature checks pass             ← session state is in Firestore, visible everywhere
    if local file exists: serve       ← only true on instance A, until it stops
    else: 404 "Instrumental MIDI file not found."
```

Production settings make "any instance" common:

| Setting | Value | Effect |
|---|---|---|
| Session store | `FirestoreSessionStore` (non-dev `APP_ENV`) | Every instance sees the session and the MIDI path |
| Session affinity | off | A user's requests are not tied to one instance |
| `containerConcurrency` / max instances | 2 / 3 | A third concurrent request starts or uses another instance |
| Min instances | 0 | Scale to zero wipes every local disk |
| `BACKEND_SESSION_TTL_SECONDS` | 5 days (default) | Sessions outlive their instances many times over |

User impact when the GET lands elsewhere:
- The score player shows "Instrumental MIDI file not found." and plays no instruments.
- The browser mix download has no instruments.
- The instrumental credit was already charged for that score.

The UI keeps a fetched MIDI as an in-memory blob URL and fetches again only
when the session changes, when the MIDI is republished for new instrumental
content (the first take with instruments after an upload or instrumental edit),
or when the player's repeat order changes (a take rendered in the other order,
or the With Repeats toggle while there is no take). Each of those fetches is a
chance to land on another instance.

What already works and stays as is:

| Artifact | How it survives |
|---|---|
| Active MusicXML | Uploaded to `sessions/{uid}/{sid}/scores/.../input.xml`; `_ensure_active_musicxml_path` re-downloads it when the local copy is missing |
| Take audio (`output.mp3`, `source.wav`) | Written to Cloud Storage; `/audio` streams from storage |
| MIDI for later takes | `_ensure_instrumental_midi_artifacts` regenerates it when the local files are missing, and republishes it on that instance only |

## 2. Decisions

| Topic | Decision |
|---|---|
| Approach | Upload both MIDI files to Cloud Storage when a job publishes them; the endpoint and the reuse check restore a missing local copy from storage. Same pattern as the active MusicXML. |
| Object path | `sessions/{uid}/{sid}/instrumental-midi/{signature}/written.mid` and `.../expanded.mid` |
| Why keyed by signature | The signature already identifies the MIDI by everything it is built from (score id, instrumental parts, timing, presets, format version). Identical content always has the same path, and a different score never collides. |
| Upload timing | After the files are finalized, before session state is published and before credits settle, inside the existing billing boundary |
| Upload failure on a billable job | The job fails and the reservation is released, as for a MIDI generation failure today ("MIDI is a billable component") |
| Upload failure on a vocal-only job (MIDI already paid) | Logged; the job still succeeds; the local copy is published as today |
| Ownership for rollback | Upload create-only (`if_generation_match=0`). Only objects this job created are deleted on rollback. An object that already existed belongs to an earlier job. |
| Session file keys | New `instrumental_midi_original_storage_path` and `instrumental_midi_expanded_storage_path`, next to the existing local-path keys |
| Job record | `performanceMidiPaths` records `originalStoragePath` / `expandedStoragePath`; the container paths are dropped |
| Local development (`SessionStore`, no storage) | Unchanged: local files only |
| Sessions published before this change | Not handled: there is no way to return to an earlier session yet. They keep today's behaviour. |
| Browser access | Unchanged: the UI fetches `/instrumental-midi` with the sign-in and App Check headers, and the backend reads storage. No signed URL or playback token, so nothing expires; the UI keeps the bytes as an in-memory blob URL. |
| Object retention | Kept indefinitely, like the take audio. No lifecycle rule. |

## 3. Design

### 3.1 Publish (job pipeline, `orchestrator.py`)

```
_finalize_instrumental_midi_files(publication)          # unchanged: staging → final local paths
if backend_use_storage:
    for kind in available kinds (written, expanded):
        object = midi_storage_path(uid, sid, signature, kind)
        created = upload_create_only(bucket, local_final_path(kind), object, "audio/midi")
        if created: publication.uploaded_objects.append(object)     # owned by this job
        paths[f"{kind}StoragePath"] = object
_publish_instrumental_midi_session_state(...)
    set_file(original_path / expanded_path)            # as today
    set_metadata(original_storage_path / expanded_storage_path)    # new
settle credits                                          # unchanged order
```

`upload_create_only` returns `False` when the object already exists, so the
second job for the same content does not re-upload or claim the object.

### 3.2 Restore (shared helper)

```
ensure_local_instrumental_midi(snapshot, kind) -> Path | None
    local = resolve allowlisted files[f"instrumental_midi_{kind}_path"]
    if local is a file: return local
    object = files.get(f"instrumental_midi_{kind}_storage_path")
    if not (backend_use_storage and object): return None
    data = download_bytes(bucket, object)               # missing object → None
    write to local via temp file + replace               # atomic, like the MusicXML restore
    return local
```

Used by:
- **GET `/instrumental-midi`:** the signature checks stay first and unchanged. `midi_path = ensure_local_instrumental_midi(...)`; `None` → 404, as today.
- **`_ensure_instrumental_midi_artifacts` reuse check:** when the marker matches, restore from storage before deciding to regenerate. A later take on another instance then reuses the published MIDI instead of rebuilding it.

### 3.3 Rollback (`finally` block of the job)

```
if publication and not committed:
    retract session state                                # as today
    delete local files this job moved                    # as today
    delete storage objects in publication.uploaded_objects   # new: only this job's own
```

### 3.4 Unchanged

| Item | Behaviour |
|---|---|
| Signature computation and the endpoint's signature checks | unchanged |
| Billing components, pricing and the instrumental charge scope | unchanged |
| UI and the API response | unchanged (the same MIDI bytes, from the same URL) |
| `performance_midi_published` in job progress | still "paths recorded" (truthiness) |
| Dev mode without storage | unchanged |

## 4. Alternatives considered

| Option | Why not |
|---|---|
| Cloud Run session affinity | Best effort only; does not survive scale to zero or instance replacement |
| `min-instances ≥ 1` | Costs a GPU instance around the clock; still loses files on deploys and restarts |
| Regenerate on every GET from the restored MusicXML | Works without storage, but repeats the MIDI build (music21) on every fetch and toggle, adds latency, and can fail at playback time after the user has paid. |
| Signed storage URL or playback token for the browser | Not needed: the request can carry the sign-in headers, and a token would add expiry handling for no benefit. The audio uses a playback token only because an `<audio>` element cannot send headers. |

## 5. Tests

| Test | Checks |
|---|---|
| Publish uploads | A billable job uploads both files to the signature path, records the storage keys in session files and the job record |
| Restore on another instance | Delete the local files (simulating instance B); the GET restores from storage and serves identical bytes |
| Reuse on another instance | A second vocal-only job with the local files deleted restores instead of regenerating (no `build_instrumental_performance_midis` call) |
| Create-only ownership | Two jobs for the same content: the second does not claim the object; rolling back the second leaves it in place |
| Rollback | A failed billable job deletes the objects it created and retracts the session keys |
| Upload failure | Billable job: fails and releases the reservation. Vocal-only job: succeeds with a log line. |
| Missing object | Storage key present but the object is gone: 404, as today |
| Dev mode | `SessionStore` without storage: no uploads; behaviour unchanged |
| Path safety | A storage key outside `sessions/{uid}/{sid}/instrumental-midi/` is refused |
