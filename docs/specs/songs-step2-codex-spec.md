# Songs, step 2: Codex task specs

2026-11-13. Differences accepted by Alan. C1 is posted to Linear as SIG-194; C2 follows when C1 is merged into the parent.

Design: `docs/specs/sing-all-process-backend-hld.md` §2, §2.1, §4, §11, §13, §15, §16 row 2, on branch `docs/sing-all-process-hld`. Step 1 (Stop) is already on the parent branch.

A Song is today's chat session: one chat, one current score, its sung tracks and its mix. Today the session id lives only in the page, so a reload starts an empty session and the work is unreachable. This step gives each Song a title and a URL, saves its player, and lets the user reopen, rename, delete and switch Songs. It ships to everyone, free and paid.

There are two Codex tasks, run one after the other, and one rules change that is not for Codex:
- **C1 Backend**: the Song fields, the player record, opening a Song, My songs, saving the mix, and Replace on upload.
- **C2 UI**: `/app/song/{id}`, the title, New song, the "New song or replace?" popup, My songs, and saving and restoring the mix. C2 starts after C1 is merged into the parent branch.
- **C3 Client rules**: a browser can no longer write job records or session files. Found while speccing the share link (step 7); it applies today.

Parent branch for both: `feature/sing-all` (1055848 or later). Codex PRs go into it, never into main.

The repository is public. Tests, fixtures, commits and PR text use made-up titles, part names and lyrics only, and no real job or session ids.

## Differences from the HLD

Accepted by Alan, 2026-11-13.

| # | HLD | This spec | Why |
|---|---|---|---|
| 1 | The player record also holds `scoreVersions`, `measureMaps`, `instrumentalMidi`, `defaultMode`, `voiceCredit` | Left out in step 2 | The session already holds them, and the owner's player reads them from there today. A second copy must be kept in sync at every score save. The share page (step 7) doesn't need one either: the backend builds that page, and it reads the session for it (`docs/specs/share-step7-codex-spec.md`) |
| 2 | Each vocal track has a `mode` (lyrics or solfège) | One track per part, no `mode` | That is today's player rule. The lyrics/solfège switch arrives with Sing All (step 3) |
| 3 | "My songs lists only docs with `songCreatedAt`" | A flag `songListed` picks the docs; `songCreatedAt` is still stored | Two equality filters need no new Firestore index, so there's no index to deploy before the backend. The Firestore emulator doesn't enforce indexes, so a missing one would only fail in production |
| 4 | My songs rows show "shared or not" and a Share action | Left out | Share is step 7 |
| 5 | Delete "removes the Song from the list" | The same, as a soft delete: the files, jobs and billing records stay | Removing stored files needs its own rule (what, and how long after). Not in this step |
| 6 | `GET /sessions/{id}/workspace` and `PUT /sessions/{id}/player/mix` | `GET /songs/{id}/workspace` and `PUT /songs/{id}/player/mix` | Both only make sense once a score is uploaded, so they belong to the Song (Alan, 2026-11-13) |

## Data structure

```text
 users/{uid}
     │ userId
     ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ sessions/{sessionId}                         ONE SONG = one chat + one score │
│                                                                              │
│  existing  userId, createdAt, lastActiveAt, scoreId, currentScoreVersion,    │
│            scoreSummary, solfegeSettings, files, currentJobId, history[], …  │
│                                                                              │
│  NEW       title          str, 1..120 chars                                  │
│            titleEdited    bool   true once the user renames the Song         │
│            songCreatedAt  time   set at the first score upload               │
│            songListed     bool   true at the first upload, false on delete   │
│            deletedAt      time or null                                       │
│                                                                              │
│  history[i] = {id, role, content, timestamp}                                 │
│               └─ a user entry's id is the chat turn id                       │
└───────┬───────────────────────────────────────────▲──────────────────────────┘
        │ subcollection                             │ sessionId, originatingTurnId
        ▼                                           │
┌──────────────────────────────────────┐   ┌────────┴─────────────────────────┐
│ sessions/{sessionId}/player/current  │   │ jobs/{jobId}          (existing) │
│ NEW, one per Song                    │   │                                  │
│                                      │   │  userId, sessionId, jobKind,     │
│  ownerUid, scoreId, revision,        │   │  status, scoreId, audioTrack,    │
│  updatedAt, repeats                  │   │  expandRepeats, outputPath,      │
│                                      │   │  actualDurationSeconds,          │
│  vocals[]       one per sung part ───┼──▶│  voicebankName, …                │
│  instruments[]  one per instrument   │   │                                  │
│                 the user has changed │   │  vocals[].sourceJobId = jobId    │
│  bus            {muted, solo,        │   │  the audio file stays with the   │
│                  levelDb}            │   │  job; the record holds no URL    │
└──────────────────────────────────────┘   └──────────────────────────────────┘
```

### `sessions/{sessionId}/player/current`

| Field | Type | Meaning |
|---|---|---|
| `ownerUid` | str | The Song's owner |
| `scoreId` | str | The score these tracks belong to |
| `revision` | int | Goes up by 1 on every write |
| `updatedAt` | time | Server time of the last write |
| `repeats` | bool | The repeat order of the takes: false = as written, true = with repeats. The player holds takes of one order only |
| `vocals[]` | list, at most 64 | One entry per sung part |
| `instruments[]` | list, at most 64 | The user's choices for an instrument part. A part with no entry plays with the player's defaults |
| `bus` | map | `{muted: bool, solo: bool, levelDb: number -48..6}` for the instrument group |

`vocals[]` entry:

| Field | From | Meaning |
|---|---|---|
| `key` | job `audioTrack.key` | `id:{partId}`, or `index:{n}` when the part has no id. One entry per key |
| `partId`, `partIndex`, `label`, `verseNumber` | job `audioTrack` | The part that was sung |
| `takeSignature` | job `audioTrack.take_signature` | The part's music when the take was sung. Null on older takes |
| `sourceJobId` | the job id | Where the audio is. The URL is signed when the Song is opened |
| `expandRepeats` | job `expandRepeats` | The take's repeat order |
| `durationSeconds` | job `actualDurationSeconds` | |
| `voiceName` | job `voicebankName` | For display |
| `muted`, `solo`, `volume` | the owner's mixer | Defaults for a new entry: not muted, not solo, the player's default volume. `volume` is 0..1.2: the vocal sliders go up to 1.2 |

`instruments[]` entry: `rawPartId`, `muted`, `solo`, `volume` (0..1), `gmProgram` (0..127 or null), `soundfontBank` (0..16383 or null), `presetKind` (str or null), `unassigned` (bool). The label and the percussion flag are not stored: they come from the score summary, as today.

### Who writes what

| Event | Session doc | Player record |
|---|---|---|
| First score upload (file or demo song) | `title`, `titleEdited = false`, `songCreatedAt`, `songListed = true`, `deletedAt = null`, plus today's reset | Created empty for the new `scoreId` |
| Upload that replaces the score | `title` follows the new score unless `titleEdited`; plus today's reset | Reset: new `scoreId`, no vocals, no instruments, default bus, `repeats = false` |
| A take completes and is settled | nothing new | `record_take` (below) |
| The owner changes the mixer or an instrument | nothing | `PUT /songs/{id}/player/mix` |
| Rename | `title`, `titleEdited = true` | nothing |
| Delete | `songListed = false`, `deletedAt = now` | nothing |
| Split, solfège, score edit | as today | nothing: stale takes are filtered when the Song is opened |

## API

All endpoints need the same auth as the chat endpoint. An unknown id, another user's Song and a deleted Song all answer 404, as session endpoints do today. JSON bodies use snake_case. Error bodies are `{"code": "...", "message": "..."}`, with the message from `backend_message`.

| Method and path | Purpose | New? |
|---|---|---|
| `GET /songs/{id}/workspace` | Everything needed to open a Song | new |
| `GET /songs` | My songs | new |
| `PATCH /songs/{id}` | Rename | new |
| `DELETE /songs/{id}` | Delete | new |
| `PUT /songs/{id}/player/mix` | Save the mixer and instrument choices | new |
| `POST /sessions/{id}/upload` | Adds `replace`; sets the title; resets the player record | changed |
| `POST /sessions`, `POST /sessions/{id}/stop`, the rest | unchanged | |

Which prefix an endpoint uses:

```python
/sessions/{id}/…   # works before a score is uploaded: create, upload, chat, stop, progress, and the other existing ones
/songs/{id}/…      # needs a Song: answers 404 song_not_found for a session with no upload. {id} is the session id
```

### `GET /songs/{id}/workspace`

404 `song_not_found` when the session isn't a Song (no `songCreatedAt`), is deleted, or isn't the caller's.

```json
{
  "session_id": "…",
  "song": {"id": "…", "title": "Example Song", "title_edited": false,
           "created_at": "2026-11-13T10:00:00Z", "last_active_at": "2026-11-13T10:20:00Z"},
  "score": {"score_id": "…", "current_score": {"version": 3},
            "score_summary": {}, "solfege_settings": {"system": "movable_do", "mode": "major", "revision": 1}},
  "messages": [{"id": "…", "role": "user", "content": "…", "timestamp": "…"}],
  "jobs": [{"job_id": "…", "job_kind": "synthesis", "status": "done", "audio_url": "…signed…",
            "originating_turn_id": "…", "created_at": "…", "progress_url": "/sessions/…/progress?job_id=…"}],
  "player": {"score_id": "…", "revision": 7, "repeats": false,
             "vocals": [{"key": "id:P1", "part_id": "P1", "part_index": 0, "label": "Part One",
                         "verse_number": 1, "take_signature": "…", "source_job_id": "…",
                         "expand_repeats": false, "duration_seconds": 93.4, "voice_name": "…",
                         "muted": false, "solo": false, "volume": 1.0,
                         "audio_url": "…signed…", "progress_url": "/sessions/…/progress?job_id=…"}],
             "instruments": [{"raw_part_id": "P5", "muted": false, "solo": false, "volume": 0.65,
                              "gm_program": 0, "soundfont_bank": 0, "preset_kind": null, "unassigned": false}],
             "bus": {"muted": false, "solo": false, "level_db": 0}}
}
```

- `score` has the same fields, built the same way, as the upload response (without `parsed`). `score_summary` is the session's current one, so it carries `performance_midi` and the measure map, and the player gets its instrument rows as it does today. The score file and the MIDI still come from `GET /sessions/{id}/score` and `GET /sessions/{id}/instrumental-midi`.
- `messages` is the saved history, in order.
- Each entry of `jobs` is the signed progress payload (`_build_signed_progress_payload`) plus `originating_turn_id`, `created_at` and `progress_url`. Audio links last 5 minutes, as today; the UI already refreshes an expired one through `progress_url`.
- `player.vocals` holds only usable tracks, each with a signed `audio_url`.

```python
def workspace_jobs(session):                       # which jobs the UI needs
    for job in job_store.list_session_jobs(user_id, session_id):       # oldest first
        if job.status in {"queued", "running"}:              yield job   # any kind: Stop, polling, upload disabled
        elif job.jobKind == "synthesis" and job.originatingTurnId \
             and job.status in {"completed", "failed"}:      yield job   # the take's bubble in the chat
        # everything else is already in the saved chat text, or isn't a chat item:
        #   a split that ended (its start and end messages are saved), a stopped take (its stop line is saved),
        #   an export mix that ended

def usable_vocals(player, session):
    if player is None or player.scoreId != session.scoreId: return []
    current = {part.part_id: part.take_signature for part in session.scoreSummary.parts}
    for t in player.vocals:
        job = the job t.sourceJobId
        if job is None or job has no audio: continue
        if t.takeSignature and current.get(t.partId) and t.takeSignature != current[t.partId]: continue   # the music was edited since
        yield t, job

# the workspace then gives each one its link: audio_url signed from job.outputPath.
# Choosing the tracks and making their links stay two steps: the share page (step 7) reuses the first with its own link.
```

### `GET /songs`

Query: `limit`, default 50, at most 200. No paging in this step.

```json
{"songs": [{"id": "…", "title": "Example Song", "created_at": "…", "last_active_at": "…",
            "status": "idle", "track_count": 2}]}
```

```python
def list_songs(user_id, limit):
    docs = sessions.where(userId == user_id).where(songListed == True)      # two equality filters, no composite index
                   .select(title, songCreatedAt, lastActiveAt, currentJobId)   # never load the history or the score
    newest lastActiveAt first; keep `limit`
    status      = "working" if the Song's currentJobId job is queued or running else "idle"   # one batched read
    track_count = len(player.vocals) if the record's scoreId is the Song's else 0             # one batched read
```

`status` is for display only. Delete checks running work again on the server.

### `PATCH /songs/{id}`

Body `{"title": "New title"}`. The title is trimmed; it must be 1 to 120 characters, else 400 `invalid_title`. Sets `titleEdited = true`. Returns the Song's row, as in `GET /songs`.

### `DELETE /songs/{id}`

```python
if job_store.list_active_jobs(user_id, session_id): return 409 work_running
session.songListed = False; session.deletedAt = now
return {"deleted": true}
```

After this, every endpoint of that session answers 404. Nothing else is removed.

### `PUT /songs/{id}/player/mix`

```json
{"score_id": "…",
 "vocals": [{"key": "id:P1", "muted": false, "solo": false, "volume": 0.8}],
 "instruments": [{"raw_part_id": "P5", "muted": false, "solo": false, "volume": 0.65,
                  "gm_program": 0, "soundfont_bank": 0, "preset_kind": null, "unassigned": false}],
 "bus": {"muted": false, "solo": false, "level_db": 0}}
```

```python
def save_mix(session, body):                       # one transaction on the player record
    if not session.songCreatedAt: return 404 song_not_found
    if body.score_id != session.scoreId: return 409 score_changed      # a late save from before a Replace
    p = record or a new empty record for session.scoreId
    for v in body.vocals:
        t = the entry of p.vocals with key v.key
        if t: t.muted, t.solo, t.volume = v.muted, v.solo, v.volume    # an unknown key is ignored: only a take adds a track
    p.instruments = body.instruments                                    # replaced as a whole
    p.bus = body.bus
    p.revision += 1; p.updatedAt = now
    return {"revision": p.revision}
```

Last write wins; there is no revision check. Validation (400 `invalid_mix`): vocal volumes 0..1.2, instrument volumes 0..1, `level_db` -48..6, `gm_program` 0..127 or null, `soundfont_bank` 0..16383 or null, strings at most 200 characters, each list at most 64 entries.

### `POST /sessions/{id}/upload`, changed

New optional form field `replace` (`"true"` when the user chose Replace). The response gains `"song": {"id", "title", "title_edited", "created_at"}`.

| Work running in the Song | `replace` | Result |
|---|---|---|
| no | any | Upload as today, plus the title and the player reset |
| yes | absent | 409 `work_running`, as today, checked before the file is read |
| yes | `true` | The file is parsed and validated first, so a bad file stops nothing. Then the work is stopped with the Stop button's own function. All stopped: the upload goes on. Any job too late or not stoppable: 409 `stop_too_late`, the score is unchanged, and that job completes as normal |

```python
def song_title_from(summary, file_name):
    return first non-empty of: summary.title, the file name without its extension, "Untitled song"   # trimmed, cut to 120

on upload commit:                                   # one transaction with the score reset
    if not session.songCreatedAt:
        songCreatedAt = now; songListed = True; deletedAt = None; titleEdited = False
        title = song_title_from(summary, file_name)
    elif not session.titleEdited:
        title = song_title_from(summary, file_name)
    player record = empty record for the new scoreId
```

A split can't be stopped yet (that is task B), so Replace during a split always answers `stop_too_late`.

---

## C1. [Codex] Songs, backend: Song fields, the player record, opening a Song, My songs and Replace

### Parent

Branch: `feature/sing-all`. PR into it, never main.
Design: the HLD sections listed at the top, on branch `docs/sing-all-process-hld`. Where this spec and the HLD differ, this spec wins.

### Rule

A Song is a session that has had a score uploaded. Everything a reopened Song shows comes from three places that already exist or are added here: the session document (chat, score, title), the jobs of that session (each take's audio and state), and the new player record (which tracks the player holds, and the mix).

The player record is filled on the server when a take is settled, so it is right even if the page was closed while the take sang.

```python
def record_take(session_id, job_id, job):          # one transaction on the player record
    if job.jobKind != "synthesis" or not job.audioTrack: return
    session = sessions[session_id]
    p = record or a new empty record for session.scoreId
    if job.scoreId != p.scoreId: return                                   # the score was replaced while it sang
    if bool(job.expandRepeats) != p.repeats:
        p.vocals = []; p.repeats = bool(job.expandRepeats)                # the player holds one repeat order
    entry = fields of the vocals table, from the job
    old = the entry of p.vocals with the same key
    if old: entry.muted, entry.solo, entry.volume = old.muted, old.solo, old.volume; replace old
    else:   append entry
    p.revision += 1; p.updatedAt = now
```

`record_take` runs after the take is completed and settled. It is wrapped so that any failure is logged as `player_record_update_failed` and the take still succeeds. Running it twice for the same job leaves the same record.

### Change

#### 1. Session stores, `src/backend/session.py`

Both stores (in memory and Firestore) get the same behaviour.

- New session fields as in the data structure. `create_session` does not set them: a session becomes a Song at its first upload.
- `commit_uploaded_score` takes the file name, sets the Song fields and the title as in "on upload commit", and resets the player record in the same transaction.
- Reads of a session with `deletedAt` set raise `KeyError`, so every session endpoint answers 404.
- New methods: `get_player`, `record_take`, `save_mix`, `rename_song`, `delete_song`, `list_songs`, each as the pseudocode above. The Firestore player record is `sessions/{id}/player/current`.
- `list_songs` uses a field projection, so it never loads a Song's history or score.

#### 2. Job store, `src/backend/job_store.py`

- `list_session_jobs(*, user_id, session_id) -> list[(job_id, data)]`: all jobs of that user and session, oldest first. It is `list_active_jobs` without the status filter; `list_active_jobs` should call it.
- A batched read of jobs by id, for the `GET /songs` status.

#### 3. Take hook, `src/backend/orchestrator.py`

Call `record_take` after a take is completed and settled, on every route that completes a take: the in-process route, the synthesis worker, the premium route, and the e2e path that completes the job without credits. Use one helper; where the routes share a completion point, call it there once.

#### 4. Endpoints, `src/backend/main.py`

- `GET /songs/{id}/workspace`, `GET /songs`, `PATCH /songs/{id}`, `DELETE /songs/{id}`, `PUT /songs/{id}/player/mix`, as in the API section.
- Upload: the `replace` field, the title, the `song` block in the response, and the table above. Move the body of the stop endpoint into one function and call it from both the stop endpoint and Replace.
- `usable_vocals` only chooses the tracks. The workspace adds the signed links in a second step.
- The e2e state endpoint may also return the player record, for tests.

#### 5. Messages, `src/backend/messages/en.properties`

`song.not_found`, `song.invalid_title`, `song.work_running`, `player.invalid_mix`, `player.score_changed`, `upload.stop_too_late`.

#### 6. A job's audio file must be inside its own folder, `src/backend/main.py`

A job document's `outputPath` is trusted today when its audio link is signed. A job's files always live in the job's own folder, so check that before signing or reading.

```python
def owned_job_path(path, *, user_id, session_id, job_id):      # storage mode only
    prefix = f"sessions/{user_id}/{session_id}/jobs/{job_id}/"
    ok = isinstance(path, str) and path.startswith(prefix) and ".." not in path.split("/")
    return path if ok else None

used by:
    _build_signed_progress_payload    resource_path = owned_job_path(job.outputPath, ids from the job document)
    the workspace's audio links       the same function, so the same check
    the export mix's source file      replaces the prefix check that is written out there today
when it returns None for a job that has an outputPath:
    the payload carries no audio_url, and one warning is logged: job_audio_path_rejected job_id=…
    (the export mix keeps raising its ValueError, as today)
```

- Passing `resource_path=None` is not enough: `_build_signed_audio_url` then falls back to the payload's own path (`_playback_resource_path`). A rejected path must remove the link.
- A player track whose job's path is rejected is left out of `player.vocals`.
- `_sign_audio_payload_urls` is also called for the chat response with no `resource_path`, so it signs the payload's own path. Give that path the same check.
- In local mode (no storage) nothing changes: the audio route already serves by file name from the session's folder.

If another place reads `outputPath` or `losslessOutputPath` from a job document to sign a link or open a file, it uses the same function. List in the PR the places you found.

### Scope

Backend only: `src/backend/`, its tests, and the messages file. No UI change, and no change to `firestore.rules` or `storage.rules` (C3). The existing UI keeps working against this backend, because every change is an addition: the new upload field is optional, and an upload with no `replace` behaves as today.

### Done when

Each new test fails on the parent commit.
- **Becomes a Song:** a first upload sets the title from the score, `songCreatedAt` and `songListed`. A score with no title takes the file name. A session with no upload is not in `GET /songs`, and its workspace answers 404.
- **Player record from a take:** a settled take adds one vocal entry with the fields in the table. A second take of the same part replaces it and keeps its mute, solo and volume. A take in the other repeat order clears the others and flips `repeats`. A take that finishes after the score was replaced changes nothing.
- **Never fails a take:** when `record_take` raises, the take still completes and is settled, and the failure is logged.
- **Closed page:** a take whose chat stream was closed by the client still completes and is in the record.
- **Workspace:** it returns the saved history, the score block, the jobs chosen by `workspace_jobs`, and the usable tracks with signed audio links. A track whose part was edited since the take is left out. A running job is in `jobs`.
- **Mix:** `PUT …/player/mix` saves the vocal mixer values, the instrument choices and the bus, and the workspace returns them. An unknown vocal key adds nothing. A wrong `score_id` answers 409 `score_changed`. Out-of-range values answer 400.
- **Replace on upload:** the title follows the new score, except after a rename. The player record is reset. With `replace` and a running take before its stop point, the take is stopped and refunded and the upload goes on. With a job that is too late, or a split, the answer is 409 `stop_too_late` and the score is unchanged. With `replace` and a bad file, nothing is stopped.
- **My songs:** `GET /songs` lists only the caller's undeleted Songs, newest activity first, with `status` and `track_count`.
- **Rename and delete:** rename trims and validates. Delete with running work answers 409. After a delete the Song is gone from the list, and its workspace, chat and upload answer 404.
- **Ownership:** every new endpoint answers 404 for another user's Song.
- **Audio path:** in storage mode, a job whose `outputPath` is outside its own folder (another session's folder, another job's folder, or a path with `..`) gets no `audio_url` in its progress payload or in the workspace, and the warning is logged. A normal take's link is signed as before. The export mix still refuses such a source.

### Checks (paste baseline vs after outputs in the PR)

```
P=/Users/alanchan/antigravity/ai-singer-diffsinger/.venv310/bin/python
FIRESTORE_EMULATOR_AUTOSTART=0 $P -m pytest tests/test_backend_api.py tests/test_job_store_progress.py tests/test_job_dispatch.py tests/test_credits.py tests/test_credit_retry.py tests/test_mcp_progress_firestore.py tests/test_mcp_server.py tests/test_mcp_end_to_end.py tests/test_premium_synthesis_worker.py tests/test_synthesis_pricing.py tests/test_audio_mix.py -q -p no:cacheprovider
```

Add your new test files to the same command. Measure the baseline on the parent commit first. Its known failures (12 in `tests/test_backend_api.py` at 1055848) are not caused by this work; do not fix them, and the same ones must remain with no new ones. `git diff --check` is clean.

### Out of scope

The UI (C2). The rules files (C3). The share link and `shareToken`. Sing All, the lyrics/solfège switch, and `mode` on tracks. The extra player record fields of difference 1. Removing a deleted Song's files. Paging in `GET /songs`. Listing sessions made before this ships. Stopping a split (task B). The `score_player_takes` field of the chat request, which the UI keeps sending as today.

---

## C2. [Codex] Songs, UI: a URL and title per Song, New song, "New song or replace?", My songs, and a saved mix

Starts after C1 is merged into `feature/sing-all`.

### Parent

Branch: `feature/sing-all`. PR into it, never main.

### Rule

The address bar says which Song is open. `/app` is the start screen with no Song; `/app/song/{id}` is one Song. Reloading, or opening the link later, shows the same Song: its chat, score, sung tracks and mix. Leaving a Song never stops its work.

```python
def open_song(id):                                 # on load of /app/song/{id}, and from My songs
    workspaceGenerationRef += 1                    # late answers for the previous Song are ignored
    close the previous Song's take stream reader   # the job itself keeps running on the server
    clear the chat, score, player and progress state
    w = GET /songs/{id}/workspace               # 404 -> "This song isn't available", with a link to /app
    restore the session id, title, score summary, solfège settings, and the score file (fetchScoreXml)
    chat = rebuild_chat(w.messages, w.jobs)
    vocal tracks = w.player.vocals, with their saved mute, solo and volume
    repeat switch = w.player.repeats
    instrument rows = derived from the score summary as today, then the saved choices applied by raw_part_id
    bus = w.player.bus
    for each job in w.jobs that is queued or running: resume polling its progress_url    # Stop works as in step 1

def rebuild_chat(messages, jobs):
    # A turn is a user message and the assistant messages after it, up to the next user message.
    # The turn id is the user message's id.
    for each message, in order: add a bubble with its text
    for each turn, for each job with originating_turn_id == turn id, oldest first:
        first job of the turn: apply it to the turn's last assistant bubble
        any other job: its own bubble, right after
        "apply" = the same function that handles a poll result today (audio, error, or progress)
    a running job with no turn (an export mix): no bubble; it only keeps Stop shown and upload disabled
```

| Action | What happens |
|---|---|
| First upload or demo song on `/app` | Uploads into the page's session as today, then the address becomes `/app/song/{id}` (replacing `/app` in the browser history) |
| Upload or demo song in a Song that has a score | The popup "New song or replace?" opens. New song is the default button |
| Popup: New song | `POST /sessions`, upload there, open `/app/song/{new id}`. The old Song is unchanged and its work keeps running |
| Popup: Replace | Upload with `replace`. On success: as an upload today (chat stays, player cleared), and the title updates. On 409 `stop_too_late`: say that the current job is finishing and can't be stopped, keep the chosen file, and open the popup again when that job ends |
| Popup: Cancel | Nothing changes |
| New song button | Goes to `/app`: the start screen with a fresh session |
| My songs | A drawer listing the Songs from `GET /songs`: title, last activity, "Working…" when `status` is `working`, and the number of sung parts. Each row has Open and Delete |
| My songs: Delete | Asks for confirmation. Disabled while the row is working. On 409 `work_running`, say so and refresh the list. Deleting the open Song goes to `/app` |
| Title | Shown in the header of an open Song. Clicking it edits it in place; Enter or leaving the field saves with `PATCH /songs/{id}`, Escape cancels |
| Mixer or instrument change | Saved with `PUT …/player/mix` about 1 second after the last change |

```python
can_leave_song = not waiting for the assistant's reply
# From sending a message until its reply text arrives or a job is accepted, New song and My songs' Open are disabled:
# a reply can't be picked up again. Once a job is running, leaving is allowed, and the job continues.

def save_mix():                                    # debounced 1 s; one request at a time, the latest state wins
    if no Song is open, or the Song is still being restored: return
    PUT /songs/{id}/player/mix with score_id, every vocal track's mute/solo/volume,
        every instrument row's choices, and the bus
    on 409 score_changed: drop it
    on any other failure: keep quiet and try again at the next change
# A pending save is sent at once when the Song is left or the tab is hidden.
```

### Change, `ui/src/App.tsx`, `ui/src/MainApp.tsx`, `ui/src/api.ts`, `ui/vite.config.ts`

1. Route `/app/song/:songId`, with the same guards as `/app`. A signed-out visitor signs in and lands on the same address.
2. `api.ts`: `fetchWorkspace`, `listSongs`, `renameSong`, `deleteSong`, `savePlayerMix`, and `replace` on `uploadScore`.
3. `open_song` and `rebuild_chat`. Restored takes reuse the existing handling of a finished poll result, so play, download, link refresh and feedback state behave as for a take made in this page. Opening a Song must not add its restored takes to the player a second time: the player's tracks come from `w.player` only.
4. The popup, the New song button, the My songs drawer, the title, and the mix save, as in the table.
5. `/app` with no Song shows what it shows today (the upload box and the demo songs) plus the My songs button.
6. Leaving a Song closes its stream reader and stops its polling without calling Stop.
7. `ui/vite.config.ts`: proxy `/songs` to the backend, as `/sessions` is. Local dev runs with an empty `VITE_API_BASE` and depends on the proxy; Playwright sets the API address itself, so no test would catch a missing entry.

### Scope

`ui/` only. Use the existing component and style patterns of `MainApp.tsx`; new components may live in their own files. No backend change: if the backend is missing something, report it instead of working around it.

### Done when

Each new Playwright test fails on the parent commit.
- **Reload:** after an upload, a chat message and a take, reloading shows the same address, title, chat, score, the take in the chat with working audio, and the track in the player.
- **Mix:** after muting a vocal track, changing an instrument and moving the bus level, a reload shows the same values.
- **New song from the popup:** uploading into a Song with a score opens the popup; New song opens a new address with the new score, and the old Song is still in My songs with its chat and track.
- **Replace:** the chat stays, the player is cleared, and the title follows the new score. After a rename, a Replace keeps the user's title.
- **My songs:** lists the Songs newest first; Open restores the Song; Delete asks first and removes the row; a working Song can't be deleted.
- **Leaving during a take:** with a take running, opening another Song keeps the take running; coming back shows its progress, and then the finished take and its track.
- **Blocked while replying:** New song and Open are disabled while the assistant's reply is awaited.
- **Not available:** an unknown or deleted Song address shows the message and the link to `/app`.
- **No double tracks:** a reopened Song with two sung parts shows exactly two vocal tracks.
- The existing Playwright specs still pass, with the same known failures as the parent.

### Checks (paste outputs in the PR)

```
cd ui && npm run build
cd ui && npx playwright test e2e/specs/<your new spec files> e2e/specs/progress-poll-recovery.spec.ts e2e/specs/synthesis-stream-recovery.spec.ts e2e/specs/core-singing-regression.spec.ts e2e/specs/score-player-takes.spec.ts e2e/specs/score-player-stale-takes.spec.ts e2e/specs/score-player-repeats.spec.ts e2e/specs/demo-songs.spec.ts
```

Measure on the parent commit first; known failures must stay the same with no new ones.

### Out of scope

Share, and the Share action in My songs. Sing All and its card. The lyrics/solfège switch in the player. Renaming from the My songs drawer. Paging or search in My songs. Restoring a demo song's suggested prompts, the selected voice, or the split-attempt details of past splits in a reopened chat: a reopened chat shows the saved text and the takes. Any backend change.

---

## C3. Client rules: a user can no longer write job records or session files from the browser

Found on 2026-11-13 while speccing the share link. **Not a Codex task:** the task protocol (§8) keeps auth and security rules away from delegated tasks. Who makes this change is Alan's call. Alan deploys it.

### What is open today

| Rule file | Today | The app uses it? |
|---|---|---|
| `firestore.rules`, `jobs/{jobId}` | A signed-in user may create, read, update and delete job documents that carry their own `userId` | No. The UI reads `users/{uid}` and `topup_packs` only. Nothing in `ui/src` or `ui/e2e` touches `jobs` |
| `storage.rules`, `sessions/{uid}/**` | A signed-in user may read and write any file under their own prefix | No. The UI never loads the Storage SDK; every file goes through the backend |

The backend writes both with the Admin SDK, which rules don't apply to.

What a user could do with it:
- Change their own job's `outputPath` to another file in the bucket, and have the backend sign and stream it. C1 change 6 closes this on the backend side.
- Change a job's `status` or other fields the backend reads back.
- Put any file under their own session folder. Once a share link exists (step 7), that is a way to serve their own audio from a SightSinger link.

### Change

```text
firestore.rules   match /jobs/{jobId}                  allow read, write: if false;
storage.rules     match /sessions/{uid}/{allPaths=**}  allow read, write: if false;
```

No backend or UI change. No data change.

### Done when

- With the emulators, a signed-in client is refused on: reading, creating, updating and deleting a `jobs` document of its own; reading and writing a file under its own `sessions/{uid}/`.
- The UI's Playwright specs pass as before: upload, split, sing, play, download, export mix.
- After the rules are deployed, one real take in production plays and downloads.

### Not checked

- Which rules are live in production. The repository has no script that deploys them.
- Whether production sets `STORAGE_BUCKET`. The default is `{project}.appspot.com`, the bucket `storage.rules` governs. With another bucket, its own access settings apply instead and need the same look.

---

## Notes for Alan

- **Two Songs can work at once.** Leaving a Song keeps its job running, and the other Song can start its own. Nothing in this step limits that; credits are reserved per job as today.
- **Sessions from before this ships aren't listed**, as the HLD says. A page that is open across the deploy becomes a Song at its next upload.
- **Replace during a split always waits**, until task B makes splits stoppable.
- **A reopened chat is close to the live one, not identical.** It shows the saved text and each take. Thought summaries and split-attempt details that were only ever in the page are not restored.
- **Deploy order:** backend (C1) before the UI (C2). No Firestore index or data migration is needed.
