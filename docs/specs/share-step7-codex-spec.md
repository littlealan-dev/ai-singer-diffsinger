# Share link, step 7: Codex task specs

2026-11-13. Differences accepted by Alan. Not posted to Linear yet: step 7 comes after steps 2 to 6.

Design: `docs/specs/sing-all-process-backend-hld.md` §14, plus §4, §13, §15 and §16 row 7, on branch `docs/sing-all-process-hld`. This step builds on Songs (step 2, `docs/specs/songs-step2-codex-spec.md`).

A share link is `https://sightsinger.app/s/{token}`. Anyone with the link can listen to the Song and follow its score, without signing in. The link is live: every page load shows the owner's latest score, sung tracks and saved mix. Viewers can't edit, chat or download.

There are three Codex tasks:
- **S1 Backend**: the share fields, turning a link on and off, and the four public routes.
- **S2 UI, player module**: move the score player out of `MainApp.tsx`, with no change in behaviour. It starts after the Songs UI task (C2) is merged.
- **S3 UI, share**: the Share button, the one-time notice, My songs, and the public page `/s/{token}`. It starts after S1 and S2 are merged.

Parent branch for all three: `feature/sing-all`. Codex PRs go into it, never into main.

The repository is public. Tests, fixtures, commits and PR text use made-up titles, part names and lyrics only, and no real job, session or share ids.

## Does the step 2 data structure support sharing?

Yes. Sharing adds three fields and one small collection, and changes no field of step 2.

The public page never reads Firestore or Storage itself. It calls the backend, and the backend reads the Song for it, from the same three places the owner's workspace uses. So the player record can stay as slim as step 2 makes it.

| The share page shows | Where it is stored | Added by |
|---|---|---|
| Title | `sessions/{id}.title` | step 2 |
| Score preview | The Song's current MusicXML file, found through `sessions/{id}.files` | exists |
| Sung tracks: label, voice, length | `player/current.vocals[]` | step 2 |
| Each track's audio | The job `vocals[].sourceJobId` → its `outputPath` | step 2 + exists |
| Which tracks are still valid | `vocals[].takeSignature` against `scoreSummary.parts[].take_signature` | step 2 + exists |
| Instrument rows, labels, percussion | `scoreSummary.performance_midi` | exists |
| The owner's instrument choices | `player/current.instruments[]` | step 2 |
| The owner's mix (mute, solo, volume, bus) | `player/current` | step 2 |
| Repeat order | `player/current.repeats` | step 2 |
| Bar positions for the cursor | `scoreSummary.performance_measure_map` | exists |
| The instrument MIDI | `sessions/{id}.files` (written and with-repeats paths) | exists |
| Voice credits | `vocals[].voiceName` | step 2 |
| Is the link on, and which Song | `sessions/{id}.shareToken`, `shareEnabled`; `shares/{token}` | **step 7** |
| The one-time notice was accepted | `users/{uid}.shareNoticeAt` | **step 7** |

This corrects one line of the Songs spec. Its difference 1 said the extra record fields of the HLD (`scoreVersions`, `measureMaps`, `instrumentalMidi`, `defaultMode`, `voiceCredit`) would be added at step 7, "because the share page can't read the session". The page can't, but the backend can, so they are not needed at step 7 either. The Songs spec is updated to say so.

Two things in the code, not the data, matter for sharing:
- **Choosing the usable tracks and signing their links are two steps.** The public routes reuse the first with a different link. This is now in the Songs spec (C1), so step 2 builds it that way.
- **Reading a Song must be possible without marking it active.** Today every session read sets `lastActiveAt`. A viewer must not do that: it would reorder the owner's My songs, and later keep a free link alive for ever. S1 adds this read; step 2 needs no change for it.

## Differences from the HLD

Accepted by Alan, 2026-11-13.

| # | HLD | This spec | Why |
|---|---|---|---|
| 1 | `POST/DELETE /sessions/{id}/share` | `POST/DELETE /songs/{id}/share` | The prefix rule agreed on 2026-11-13: an endpoint that needs a Song is under `/songs/` |
| 2 | `shares/{token}` holds `enabled` | `enabled` is on the Song (`shareEnabled`). `shares/{token}` only maps a token to its Song | The public route reads the Song anyway. One place to write means on/off can't disagree between two documents, and My songs gets "shared" from the fields it already reads |
| 3 | `GET /public/s/{token}` returns rows already flattened by the server (`flatten`) | It returns the owner's saved mix as it is. The page works out each row's starting on/off and level | The instrument defaults (which sound a part plays with before the owner changes it) are worked out in the UI today. The page runs the owner's own code on the same data, so the two can't drift apart. `flatten` is the same rule, moved to the page |
| 4 | `/public/s/{token}/audio/{trackKey}` | `/public/s/{token}/audio?track={key}` | A key is `id:{partId}`, and a part id can hold characters that are unsafe in a path |
| 5 | The payload has `modes`, `defaultMode` and `credits` with a credit text | No modes. Credits are the voice names, with a link to the credits page | The lyrics/solfège switch arrives with Sing All (step 3). No credit text is stored anywhere today |
| 6 | The first-share notice is "one-time" | One-time per user, recorded on the server as `users/{uid}.shareNoticeAt` | It is a rights notice, so the record should not live only in one browser |
| 7 | Not in the HLD | A setting `SHARE_LINKS = off / allowlist / on` | Step 7 is allowlist-only until launch. `off` also stops every existing link at once |

## Data structure

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ sessions/{sessionId}                                    ONE SONG (step 2)    │
│                                                                              │
│  step 2    title, titleEdited, songCreatedAt, songListed, deletedAt          │
│                                                                              │
│  NEW       shareToken    str    made at the first Share, never changed       │
│            shareEnabled  bool   true = the link works                        │
└───────▲──────────────────────────────────────────────────────────────────────┘
        │ sessionId
┌───────┴──────────────────────────────┐      ┌─────────────────────────────────┐
│ shares/{shareToken}      NEW         │      │ users/{uid}          (existing) │
│                                      │      │                                 │
│  sessionId   the Song                │      │  NEW  shareNoticeAt  time       │
│  ownerUid    the Song's owner        │      │       when the owner accepted   │
│  createdAt                           │      │       the share notice          │
│                                      │      └─────────────────────────────────┘
│  written once, never changed         │
└──────────────────────────────────────┘

 Read by the public routes, written by nobody new:
   sessions/{id}                 title, scoreId, scoreSummary, files, currentScore
   sessions/{id}/player/current  vocals, instruments, bus, repeats        (step 2)
   jobs/{jobId}                  outputPath of each track's take
```

| Field | Type | Meaning |
|---|---|---|
| `sessions/{id}.shareToken` | str or absent | `secrets.token_urlsafe(16)`: 22 characters, 128 random bits. A Song has at most one, for life |
| `sessions/{id}.shareEnabled` | bool | The only on/off switch |
| `shares/{token}.sessionId`, `ownerUid`, `createdAt` | | The lookup from a token to its Song. Clients can't read it: `firestore.rules` has no rule for `shares`, so it is denied |
| `users/{uid}.shareNoticeAt` | time or absent | Set once. Clients can't write it: the rules let a user change only `metadata` |

### Who writes what

| Event | Song document | `shares/{token}` |
|---|---|---|
| First Share of a Song | `shareToken`, `shareEnabled = true` | Created, in the same transaction |
| Share again after turning it off | `shareEnabled = true` | nothing |
| Turn sharing off | `shareEnabled = false` | nothing |
| Delete the Song (step 2) | `deletedAt`, as in step 2 | nothing: a deleted Song's link answers "turned off" |
| Replace the score, a new take, a mix change | as in step 2 | nothing: the next page load shows it |
| A viewer opens the page or plays | **nothing, not even `lastActiveAt`** | nothing |

## API

### For the owner

Same auth as the chat endpoint. 404 `song_not_found` as in step 2.

| Method and path | Purpose |
|---|---|
| `POST /songs/{id}/share` | Turn the link on, and return it |
| `DELETE /songs/{id}/share` | Turn the link off |
| `GET /songs/{id}/workspace` | `song` gains `share` |
| `GET /songs` | Each row gains `shared` |

```python
def can_share(user):                               # the SHARE_LINKS setting
    off        -> False
    allowlist  -> user is on the maintenance allowlist (app_config/maintenance), the one premium routing uses
    on         -> True

def share_on(song, user, body):                    # POST, body {"notice_accepted": true} or {}
    if not can_share(user): return 403 share_not_available
    if not user.shareNoticeAt:
        if not body.notice_accepted: return 409 share_notice_required
        user.shareNoticeAt = now
    in one transaction:
        if not song.shareToken:
            song.shareToken = secrets.token_urlsafe(16)
            shares[song.shareToken] = {sessionId, ownerUid, createdAt: now}
        song.shareEnabled = True
    return {"enabled": true, "url": f"{settings.share_base_url}/s/{song.shareToken}"}

def share_off(song):                               # DELETE; fine to call twice
    song.shareEnabled = False
    return {"enabled": false, "url": null}
```

- `share_base_url` is a new setting, `SHARE_BASE_URL`, default `https://sightsinger.app`.
- Workspace: `"song": {…, "share": {"available": true, "enabled": true, "url": "https://sightsinger.app/s/…"}}`. `available` is `can_share(user)`. `url` is null while the link is off.
- `GET /songs` rows: `"shared": true` when `shareEnabled`. `list_songs` adds `shareEnabled` to its field projection; no extra read.
- The server doesn't check that the Song has a sung track. The UI only offers Share when it has one, and a link with nothing sung is a normal state anyway (after Replace).

### Public, no sign-in

| Method and path | Returns |
|---|---|
| `GET /public/s/{token}` | The page's data (JSON) |
| `GET /public/s/{token}/score` | The current score, MusicXML |
| `GET /public/s/{token}/midi?expand_repeats=true\|false` | The instrument MIDI in that repeat order |
| `GET /public/s/{token}/audio?track={key}` | One sung track, streamed |

Every public response carries `Cache-Control: no-store` and `X-Robots-Tag: noindex, nofollow`.

```python
def resolve_share(token, request, kind):           # the first lines of all four routes
    if over_limit(client_ip(request), kind): raise 429 rate_limited, with Retry-After
    if settings.share_links == "off": raise 404 share_off
    share = shares[token]
    if share is None: raise 404 share_off
    song = sessions.read_shared(share.sessionId)   # a field mask without the chat history; never writes lastActiveAt
    if song is None or song.deletedAt or not song.shareEnabled \
       or song.shareToken != token or song.userId != share.ownerUid:
        raise 404 share_off
    return share, song
```

An unknown token, a link that was turned off and a deleted Song all give the same answer, so a token can't be tested for existence.

#### `GET /public/s/{token}`

```json
{
  "title": "Example Song",
  "is_owner": false,
  "song_id": null,
  "score": {"version": 3, "url": "/public/s/…/score",
            "summary": {"duration_seconds": 93.4, "expanded_duration_seconds": 120.1,
                        "repeat_expansion_available": true,
                        "performance_measure_map": {"written": [], "expanded": []},
                        "performance_midi": {"version": 1, "instrumental_parts": [], "has_instrumental_parts": true,
                                             "original_midi_available": true, "expanded_midi_available": true}}},
  "player": {"repeats": false,
             "vocals": [{"key": "id:P1", "label": "Part One", "verse_number": 1, "duration_seconds": 93.4,
                         "voice_name": "Voice A", "muted": false, "solo": false, "volume": 1.0,
                         "audio_url": "/public/s/…/audio?track=id%3AP1"}],
             "instruments": [{"raw_part_id": "P5", "muted": false, "solo": false, "volume": 0.65,
                              "gm_program": 0, "soundfont_bank": 0, "preset_kind": null, "unassigned": false}],
             "bus": {"muted": false, "solo": false, "level_db": 0},
             "midi_url": "/public/s/…/midi?expand_repeats=false"},
  "credits": {"voices": ["Voice A"]}
}
```

```python
def share_page(token, request):
    share, song = resolve_share(token, request, "page")
    p = get_player(share.sessionId)
    vocals = usable_vocals(p, song)                # the function the owner's workspace uses; a stale take is left out
    viewer = the uid of the Authorization header, if it is there and valid, else None
             # a missing or bad sign-in token is a signed-out viewer, never a 401
    is_owner = viewer == share.ownerUid
    return the shape above
```

- **A whitelist, not a copy.** `score.summary` holds exactly the five fields shown, copied from the Song's score summary. Vocals hold exactly the fields shown: no `source_job_id`, no `take_signature`, no `part_id`. Nothing in the payload holds a user id, a session id, a job id or a storage path. `song_id` is filled only when `is_owner`.
- `midi_url` is null when the score has no instrument part or no MIDI for the player's repeat order.
- `credits.voices` is the distinct `voice_name` of the usable vocals, in track order.

#### `GET /public/s/{token}/audio?track={key}`

```python
def share_audio(token, track, request):
    share, song = resolve_share(token, request, "audio")
    t = the entry of usable_vocals(get_player(share.sessionId), song) with key == track
    if t is None: raise 404 share_track_not_found
    job = jobs[t.sourceJobId]
    path = owned_job_path(job.outputPath, user_id=share.ownerUid, session_id=share.sessionId, job_id=t.sourceJobId)
    if job.sessionId != share.sessionId or path is None:
        raise 404 share_track_not_found            # only ever a file inside this take's own folder (the Songs C1 check)
    return _stream_storage_audio(request, settings, path, download=False)    # byte ranges work as today
```

In local mode (no storage) the file is read from the session's folder, as the owner's audio route does. The route never sends `Content-Disposition: attachment` and ignores `download`.

#### `GET /public/s/{token}/score` and `/midi`

The same answers as `GET /sessions/{id}/score` and `GET /sessions/{id}/instrumental-midi`, for the Song behind the token, read on the owner's behalf without marking the Song active. Move each endpoint's body into one function and call it from both routes.

#### Limits

```python
# per backend instance, per client IP, counted over the last 60 seconds; both numbers are settings
"page"  (page data, score, midi):  SHARE_PAGE_LIMIT_PER_MINUTE,  default 240
"audio":                           SHARE_AUDIO_LIMIT_PER_MINUTE, default 1200
```

The defaults are high on purpose: a choir at a rehearsal is one IP address (one Wi-Fi), and 30 singers opening an 8-part Song is about 90 page requests and 240 audio requests within a minute. The limit is there to stop a runaway script, not to shape normal use.

The public routes keep the App Check gate that every route has (it needs no sign-in). The page loads the takes with its own `fetch`, so the audio route keeps it too; `_should_require_app_check` is not changed.

#### What a link shows

| The Song's state | Page data | The page shows |
|---|---|---|
| Link on, parts sung | 200 | The score and the player |
| Unknown token, link turned off, Song deleted, `SHARE_LINKS=off` | 404 `share_off` | "This link was turned off" |
| The score was replaced and nothing is sung yet | 200, `vocals: []` | The new score, the instruments if it has any, and "No sung parts yet" |
| A part was edited after its take | 200, without that track | The other tracks |
| A take or a Sing All is running | 200 | The tracks finished so far; a refresh shows more |
| Too many requests | 429 `rate_limited` | "Too many requests. Try again in a minute." |

---

## S1. [Codex] Share, backend: the share link, turning it on and off, and the public routes

### Parent

Branch: `feature/sing-all`, after the Songs backend task (C1) is merged into it. PR into it, never main.
Design: HLD §14, on branch `docs/sing-all-process-hld`. Where this spec and the HLD differ, this spec wins.

### Rule

A share link is a second, read-only way into a Song. It adds no copy of anything: the public routes read the Song's own documents and files at each request, so the page always shows what the owner has now.

A viewer leaves no trace on the Song. The link itself is the permission: whoever has it can listen, and the owner can end that at any time.

### Change

#### 1. Settings, `src/backend/config/`

`SHARE_LINKS` (`off`, `allowlist`, `on`; default `off`; any other value fails at start-up, as `SYNTHESIS_PREMIUM_ROUTING` does), `SHARE_BASE_URL`, `SHARE_PAGE_LIMIT_PER_MINUTE`, `SHARE_AUDIO_LIMIT_PER_MINUTE`.

#### 2. Session stores, `src/backend/session.py`

Both stores (in memory and Firestore) get the same behaviour.
- `share_on` and `share_off`, as the pseudocode. The Firestore `shares/{token}` document is created in the same transaction that sets `shareToken`.
- `get_share(token)`.
- `read_shared(session_id)`: the Song's data without the chat history, and with no write. Today `_read_owned` sets `lastActiveAt` on every read. Give that read path a way to skip the write, and use it for everything the public routes call, including the score file and the MIDI.
- `list_songs` adds `shareEnabled` to its projection.

#### 3. The notice, on the user document

`shareNoticeAt` is read and set where the backend already reads and writes `users/{uid}`. Reuse that code; don't add a second user store.

#### 4. Endpoints, `src/backend/main.py`

- `POST` and `DELETE /songs/{id}/share`; `share` in the workspace's `song`; `shared` in `GET /songs`.
- The four public routes, each starting with `resolve_share`.
- The score and MIDI endpoint bodies become functions used by the owner's and the public route.
- `usable_vocals` is used by the workspace and by the public page and audio routes. It is one function.
- The limiter: a small in-memory counter per (IP, kind) with a 60-second window, cleared of old entries as it goes. No new dependency.
- `can_share` reads the allowlist the way `premium_routing.py` does, with its cache.

#### 5. Messages, `src/backend/messages/en.properties`

`share.off`, `share.not_available`, `share.notice_required`, `share.track_not_found`, `share.rate_limited`.

### Scope

Backend only: `src/backend/`, its tests, and the messages file. No UI change, no `firestore.rules` change. Every change is an addition: with `SHARE_LINKS=off`, the default, the owner's endpoints answer `share_not_available`, the public routes answer `share_off`, and nothing else behaves differently.

### Done when

Each new test fails on the parent commit.
- **Turn on:** the first `POST` without `notice_accepted` answers 409 `share_notice_required` and creates nothing. With it, it sets `shareNoticeAt`, creates the token and the `shares` document, and returns the link. A later Song of the same user needs no `notice_accepted`.
- **Same link:** turning a link off and on returns the same URL. Two Songs get different tokens.
- **Turn off:** after `DELETE`, all four public routes answer 404 `share_off`.
- **One answer:** an unknown token, a turned-off link, a deleted Song and `SHARE_LINKS=off` give the same status and body.
- **Allowlist:** with `SHARE_LINKS=allowlist`, a user who is not on the list gets 403 `share_not_available` and `share.available = false`. A link made by a listed user opens for a signed-out viewer.
- **Page data:** it returns the title, the whitelisted summary fields, the usable tracks with their saved mix, the instrument choices, the bus and the repeat order. A track whose part was edited since its take is absent. After a Replace the same link returns the new score and no vocals.
- **Nothing leaks:** the page data's JSON text contains no user id, session id, job id or storage path of the Song. A signed-in viewer who is not the owner gets `is_owner: false` and `song_id: null`. The owner gets `true` and the id. A request with a broken sign-in token is answered as signed-out.
- **Audio:** the route returns the take's bytes and honours a byte range. An unknown `track`, a stale track, and a job whose `outputPath` is outside the Song's own folder answer 404. No response has an attachment header, with or without `download=1`.
- **Score and MIDI:** the public routes return the same bytes as the owner's routes.
- **No trace:** after the page data, score, MIDI and audio are all read through the public routes, the Song's `lastActiveAt` is unchanged and its place in `GET /songs` is the same.
- **Limits:** the request after the limit answers 429 with `Retry-After`. Another IP is not affected. The window passes and requests work again.
- **Headers:** every public response has `Cache-Control: no-store` and `X-Robots-Tag: noindex, nofollow`.
- **My songs and workspace:** `shared` and `share` follow on and off.

### Checks (paste baseline vs after outputs in the PR)

```
P=/Users/alanchan/antigravity/ai-singer-diffsinger/.venv310/bin/python
FIRESTORE_EMULATOR_AUTOSTART=0 $P -m pytest tests/test_backend_api.py tests/test_job_store_progress.py tests/test_job_dispatch.py tests/test_credits.py tests/test_credit_retry.py tests/test_mcp_progress_firestore.py tests/test_mcp_server.py tests/test_mcp_end_to_end.py tests/test_premium_synthesis_worker.py tests/test_synthesis_pricing.py tests/test_audio_mix.py -q -p no:cacheprovider
```

Add your new test files and the Songs test files to the same command. Measure the baseline on the parent commit first. Its known failures are not caused by this work; do not fix them, and the same ones must remain with no new ones. `git diff --check` is clean.

### Out of scope

The UI (S2, S3). Free links stopping after 30 days (step 8). The lyrics/solfège switch and `mode` (step 3). A "make a new link" action. Counting views. Any change to `firestore.rules` or `storage.rules`. A limit shared between backend instances.

---

## S2. [Codex] Share, UI: move the score player out of `MainApp.tsx`, with no change in behaviour

Starts after the Songs UI task (C2) is merged into `feature/sing-all`. It doesn't need S1.

### Parent

Branch: `feature/sing-all`. PR into it, never main.

### Rule

The share page plays a Song with the same engine as the owner's player. That engine and the score renderers sit inside `ui/src/MainApp.tsx` today, where another page can't import them. This task moves them and changes nothing a user can see.

```python
moved to ui/src/player/ (new files, exported):
    the engine:      ScorePlayerEngine, ScorePlayerEngineBridge, ScorePlayerMixerBridge, ScorePlayerSeekBar,
                     and the types and helpers only they use
    the score:       HorizontalScoreRenderer, the page-view renderer and its zoom helpers, the cursor-follow scrolling
    the track state: InstrumentalTrackState, InstrumentalBusState, DEFAULT_INSTRUMENTAL_BUS, the bus dB limits,
                     instrumentalTrackKey, instrumentalTrackForMidiTrack, dbToGain,
                     decodeVocalAudio, hasAudibleScorePlayerMix,
                     and the function that makes the instrument rows from performance_midi
stays in MainApp.tsx:
    everything about chat, jobs, takes arriving, link refresh, the instrument picker, export mix, feedback, and the page's own state
```

A function is moved as it is: same name, same body, same props. If a piece can't be moved without changing it, leave it and say so in the PR.

### Scope

`ui/src/MainApp.tsx` and new files under `ui/src/player/`. No change to behaviour, markup, class names or test ids. No backend change.

### Done when

- `MainApp.tsx` imports the pieces above from `ui/src/player/`, and no longer defines them.
- `npm run build` passes with no new warning.
- The Playwright specs below pass with the same known failures as the parent, with no spec file changed.

### Checks (paste outputs in the PR)

```
cd ui && npm run build
cd ui && npx playwright test e2e/specs/core-singing-regression.spec.ts e2e/specs/score-player-takes.spec.ts e2e/specs/score-player-stale-takes.spec.ts e2e/specs/score-player-repeats.spec.ts e2e/specs/demo-songs.spec.ts e2e/specs/progress-poll-recovery.spec.ts e2e/specs/synthesis-stream-recovery.spec.ts
```

Add the Songs UI specs from C2. Measure on the parent commit first.

### Out of scope

The share page. Any new prop, option or "read-only" switch on the moved pieces: S3 adds what it needs.

---

## S3. [Codex] Share, UI: the Share button, the notice, My songs, and the public page

Starts after S1 and S2 are merged into `feature/sing-all`.

### Parent

Branch: `feature/sing-all`. PR into it, never main.

### Rule

The owner shares from the app. Everyone else gets a page that only plays.

```python
# In the app
share_button_shown = song.share.available and (the player has a sung track or song.share.enabled)

def on_share_click():
    if song.share.enabled: open the share box (the link, Copy, Turn off sharing); return
    r = POST /songs/{id}/share {}
    if r is 409 share_notice_required:
        show the notice; on "Share": r = POST /songs/{id}/share {"notice_accepted": true}; on Cancel: return
    open the share box with r.url

def on_turn_off():                                 # asks first: "Anyone with the link will see 'This link was turned off'."
    DELETE /songs/{id}/share; close the box

# The public page, /s/{token}
def open_share_page(token):
    d = GET /public/s/{token}                      # with the sign-in token if the viewer happens to be signed in
        404 -> "This link was turned off"; 429 -> "Too many requests. Try again in a minute."
    score   = GET d.score.url                      # rendered by the same renderer as the app
    rows    = start_rows(d.player, d.score.summary.performance_midi)
    takes   = each vocal's audio_url, fetched and decoded as the app does, with the App Check header
    midi    = GET d.player.midi_url, only if at least one instrument row exists
    the soundfont loads on the first Play, only if an instrument row is on; its progress is shown

def start_rows(player, performance_midi):
    instruments = the app's own instrument rows from performance_midi, with player.instruments applied by raw_part_id
    instruments = [i for i in instruments if not i.unassigned]        # a kit that was never chosen is not a row
    any_inst_solo = any(i.solo for i in instruments)
    for i in instruments:                                             # the bus rule of the app's player
        i.muted = player.bus.muted or i.muted
        i.solo  = i.solo if any_inst_solo else player.bus.solo
        i.level = i.volume * dbToGain(player.bus.level_db)
    for v in player.vocals: v.level = v.volume
    rows = player.vocals + instruments
    any_solo = any(r.solo for r in rows)
    for r in rows: r.on = (r.solo and not r.muted) if any_solo else not r.muted
    return rows
```

`start_rows` is a pure function with its own unit test. The viewer's switch on a row only flips `on` in their browser. Nothing is saved, and a reload returns to the owner's mix.

| The owner's setting | On the share page |
|---|---|
| Volume, bus level | The row's fixed level; no fader |
| Mute | The row starts off; the viewer can turn it on |
| Solo | The soloed rows start on, the rest off; the viewer can change that |
| Instrument choice | The MIDI plays with the owner's instrument; no selector |
| Repeat order | The order of the owner's takes; no switch |

| The page has | The page doesn't have |
|---|---|
| The title; a Sign in button (to `/app`) | Chat |
| The score preview with the moving cursor | The instrument section |
| Play, pause, seek, master volume | Faders, solo buttons, the repeat switch |
| An on/off switch per row | Any download: no track download, no export mix |
| The voice names, linking to `/credits-and-ai-voices` | Any editing |
| A Share button that copies the page's own link | |
| For the owner only: "Open in SightSinger", to `/app/song/{song_id}` | |

**The notice** (shown once per user, before their first link):

> Anyone with this link can listen to this song and see its score. They'll also see changes you make later. Share only music you have the right to share. The voices' terms of use apply to shared recordings.

Buttons: Share, Cancel.

### Change

1. `ui/src/api.ts`: `shareSong`, `unshareSong`, `fetchSharePage`, and loaders for the public score, MIDI and audio. All send the App Check header. `fetchSharePage` also sends the sign-in token when there is one.
2. `ui/src/MainApp.tsx`: the Share button near the score preview, the notice, and the share box. My songs: a "Shared" mark on a row with `shared`, and a Share action that opens that Song's share box.
3. `ui/src/App.tsx`: the route `/s/:token`, outside `ProtectedRoute`, `BackendReadinessGate` and `MaintenanceGate`. It must not create a session, and must not call any endpoint except the four public ones.
4. A new page component under `ui/src/share/`, built from the pieces of `ui/src/player/`. It sets `<meta name="robots" content="noindex, nofollow">` and the document title to the Song's title.
5. `firebase.json`: rewrite `/s/**` to `/app.html`, as `/app/**` is, placed before the catch-all; and for `/s/**` the headers `Cache-Control: public, max-age=0, must-revalidate` and `X-Robots-Tag: noindex, nofollow`.
6. `ui/vite.config.ts`: proxy `/public` to the backend, as `/sessions` is, so the page works in local dev where `VITE_API_BASE` is empty.

### Scope

`ui/`, plus the `/s/**` entries of `firebase.json`. No backend change: if the backend is missing something, report it instead of working around it.

### Done when

Each new Playwright test fails on the parent commit. The tests run the backend with `SHARE_LINKS=on`.
- **Share:** in a Song with a sung track, Share shows the notice the first time, then the link. A second Song of the same user shows the link with no notice. Copy puts the link on the clipboard.
- **The page, signed out:** opening the link in a fresh browser context shows the title, the score and one row per sung part, and Play plays. No request goes to `/sessions` or `/songs`.
- **The owner's mix:** with one part muted by the owner, that row starts off and the viewer can turn it on. With one part soloed, only that row starts on. After a reload the rows are back at the owner's mix.
- **Live:** after the owner mutes another part, a reload of the share page shows it off. After a Replace, the same link shows the new score and "No sung parts yet".
- **Turned off:** after Turn off, the link shows "This link was turned off", and the row in My songs loses its "Shared" mark. Sharing again gives the same link.
- **Owner:** the owner, signed in, sees "Open in SightSinger" and it opens the Song. Another signed-in user doesn't see it.
- **Nothing to download or edit:** the page has no download control, fader, instrument picker or chat.
- **No soundfont without instruments:** a Song with only sung parts never requests `/soundfonts/`.
- **Not offered:** with `share.available = false` there is no Share button.
- The existing Playwright specs still pass, with the same known failures as the parent.

### Checks (paste outputs in the PR)

```
cd ui && npm run build
cd ui && npx playwright test e2e/specs/<your new spec files> e2e/specs/core-singing-regression.spec.ts e2e/specs/score-player-takes.spec.ts e2e/specs/score-player-stale-takes.spec.ts e2e/specs/score-player-repeats.spec.ts e2e/specs/demo-songs.spec.ts
```

Add the Songs UI specs from C2. Measure on the parent commit first; known failures must stay the same with no new ones.

### Out of scope

The lyrics/solfège switch (step 3). Link expiry and any "expires on" text (step 8). A view counter. Social preview images. Embedding the player in other sites. Any backend change.

---

## Later steps, and what they need from the data

| Step | Adds to sharing | Data it needs | There already? |
|---|---|---|---|
| 3, Sing All | The lyrics/solfège switch on the page | `mode` on each vocal, and the score version of each mode | No. Step 3 adds both for the owner's player; the public payload then gains `modes` and the default one |
| 8, expiry | A free user's link stops 30 days after the owner's last change | The time of the owner's last change, and the owner's plan | Nearly. `lastActiveAt` is the owner's last visit or change, which is kinder than "last change"; S1 keeps viewers from moving it. The plan is on `users/{uid}`. A strict "last change" would be one new field, added at step 8 |
| 9, launch | Everyone can share | `SHARE_LINKS=on` | Yes |

## Notes for Alan

- **Turning a link off and on gives the same link back**, as the HLD says. Anyone who kept it can listen again. A "make a new link" action would be a small addition; it's not in this step.
- **"No download" is a rule of the page, not of the network.** The page has no download control, but a browser has to receive the audio to play it, so a determined person can save it. That is true of any web player.
- **The limits count per backend instance.** With several instances running, the real ceiling is that many times higher. A hard limit needs Cloud Armor or a shared counter, and can wait until there is traffic to protect.
- **`_client_ip` takes the first address in `X-Forwarded-For`.** A caller can put anything there, so the limit can be dodged by a script that knows this. Which entry Cloud Run itself adds should be checked on prod before the limit is relied on. S1 uses the existing helper as it is.
- **Songs sung before step 2 ships have no player record**, so their link would show the score with no audio until a part is sung again.
- **Client rules come first.** Today a browser can write its own job records and session files. The Songs spec's C3 closes that, and it should be deployed before any share link exists.
- **Still to test on a real phone before launch**, as the HLD says: the 142 MB soundfont's download time and memory.
- **Deploy order:** backend (S1) with `SHARE_LINKS=allowlist`, then the UI with `firebase.json` (S3). No Firestore index or data migration is needed.
