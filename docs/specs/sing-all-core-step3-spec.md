# Sing All core, step 3: task specs

2026-11-14; S1 and S2 rewritten on 2026-11-15 for the one quote call; its third and fourth questions (voice part or instrument, and an instrument's sound) were added the same day. Nothing here is built yet. Alan accepted every difference from the HLD below on 2026-11-15; the questions under "Notes for Alan" are still open.

Design: `docs/specs/sing-all-process-backend-hld.md` §3 to §8, §15, §16 row 3, §17, on branch `docs/sing-all-process-hld`. Step 1 (Stop) and step 2 (Songs) are built.

Step 3 makes the "Sing All" button real for every signed-in user; a free plan sees "Upgrade to sing" in place of "Sing it". There is no Sing All allowlist: for the smoke test Alan turns maintenance mode on, so only the maintenance allowlist can sign in, then turns it off and Sing All is live for everyone on the same code (Alan, 2026-11-15). The user taps it, sees which parts will be sung and what it costs, and presses "Sing it". The backend then splits the parts that need it, adds solfège if asked, sings every part, and puts each finished take in the Song's player. The user can leave and come back; the progress is on the server.

Step 3 has no mix files, no zip, no email, no pause for a decision and no retry. Those are steps 4, 5 and 6.

The tasks, in build order. They are built in this session, not by Codex:

- **B. Stop a split.** Left out of step 1. A running split can be stopped before it saves.
- **S1. Quote and parts estimate.** `POST /sing-all/quote`: the parts, the voices and the exact price. One LLM call per score chooses each row's voice, says how many lines a staff has where it can be read two ways, says whether a part with no words and no declared instrument is a voice part, and chooses a sound for each instrument that has none.
- **S2. Parent job, credits and the reconciler.** `POST /sing-all/jobs`, the stages, the child jobs, per-item reserve, settle and refund, and what is refused while a Sing All runs.
- **S3. Progress and UI.** `GET /sing-all/active`, the real card in the chat, the header pill, and the lyrics/solfège switch in the player.
- **S4. Stop for the parent, and the sweeper.**
- **S5. The render job and its line.** One RTX batch run per Sing All, one at a time.
- **S6. Deployment.** Alan's task: the queue, the render job, the scheduler, and premium routing off.

Branch: `feature/sing-all-prototype`, after merging `origin/feature/sing-all` (e85579e) into it for the lane slot change (SIG-190). Nothing goes to main.

The repository is public. Tests, fixtures and commits use made-up titles, part names and lyrics only, and no real job or session ids.

## Differences from the HLD

Alan accepted rows 1 to 17 on 2026-11-15, and decided row 18 the same day. Rows 1, 4 to 7, 9 and 14 to 17 follow what he decided that day: one call at the quote for the voices, the number of lines, whether an undeclared part with no words is a voice part and an instrument's sound, tried on the normal model first; every part not sung is a MIDI track; style notes applied on "Apply"; the instrumentals' own reservation.

| # | HLD | This spec | Why |
|---|---|---|---|
| 1 | The quote has no LLM call, takes well under a second, and picks voices with "today's recommended-voice logic" | The first quote for a score makes one LLM call. It picks each row's voice by the chat model's voicebank rule, and for a staff that can be read two ways it answers shared or separate. The answer is saved with the Song and reused until the score or the list of voices changes. If the call fails or its answer is still invalid after one re-ask, the card says the quote couldn't be made, with "Try again" | Today that voice logic is only a rule in the chat model's prompt, and it reads the part's name, which code can't do in every language. Asking the lane question in the same call makes the quote exact (row 14) |
| 2 | A staff with `most == 1` is "sung as it is" | A part needs a split when the synthesiser would refuse it as it is: it has more than one voice, or its words sit on another part. The lane counts still say how many rows it gives | Two staves with one line each give `most == 1` on both, yet the part can't be sung unsplit. Using the synthesiser's own test means the quote and the take can't disagree |
| 3 | Not covered | A part already split in chat is not split again. Its existing split parts are the rows. This holds only when the chosen verse is the verse the score is loaded with | Splitting again costs LLM time and can give a different result from the one the user already reviewed |
| 4 | "`is_vocal(part)`" decides what is sung | With words, or a declared voice sound: sung. With a declared instrument and no words: an instrument. With neither: the quote call answers voice, instrument or unsure, by the chat prompt's rule for such a part. Voice: ticked, and sung in solfège. Instrument or unsure: listed unticked with a note, and the user can tick it. A part the chat model has already ruled on keeps that ruling and isn't asked about | Only an LLM can tell today, and Sing All has no chat turn to ask it in. Asking in the quote call adds no call. Unsure stays unticked because singing an accompaniment by mistake costs credits, and leaving a voice out costs one tick |
| 5 | The instrumental item is charged "once per job" | Once per score, as today: today's instrumentals rate (1 credit per started 2 minutes), held in its own reservation, charged when the first take succeeds and refunded if none does; nothing if this score's instrumentals were already paid for in chat (Alan, 2026-11-15) | Today's charge record is per score. Charging per job would charge a user twice for the same instrumentals. Riding on one take's reservation, as a chat take does, would refund it when that one take fails though others succeed |
| 6 | Instruments with no usable sound are settled by the chat model | The quote call chooses the sound, by the chat prompt's rule, for each instrument the score gives no usable sound and for each part it judges an instrument. "Sing it" saves those sounds and roles into the score summary with the function the chat's `synthesize` uses, so the chat model isn't asked again. Every part that isn't sung is a MIDI track: only a vocal row the user unticks is left out. When the call can't choose a sound, or its sound fails the check twice, the track is still included with no instrument chosen: muted, and the player's instrument dropdown says "Choose an instrument" (Alan, 2026-11-15) | Sing All has no chat turn to ask in, and the same call adds no call. Saving the answers keeps chat and Sing All agreeing on what each part is. The user can always pick the instrument in the player |
| 7 | `choices.styleNote`, and a default voice style | A style note under Customise. Customise's button is "Apply", which always folds back to the summary; a changed note is turned into each row's voice controls by one small style call then (rule S), and the summary shows them as changes from each voice's default, e.g. "gender −10 · airiness +10". Customise shows no values and makes no style call while the user types. With no note, and for a row the note doesn't concern, each take uses the voicebank's defaults and its default voice colour, not a colour chosen for the song as chat does (Alan, 2026-11-15) | The user sees what will be sung before paying, with no live updates and one call per Apply. A separate call keeps the voices and lines fixed while the note is edited |
| 8 | The quote follows "the same consent rule as today" (`presentedTurnId`) | The card shows the quote and "Sing it" sends its id. The backend prices the request again and starts only if the parts and the total are the same; otherwise it answers `quote_changed`, and the card asks for a new quote with the same choices | Today's rule is about chat turns: quote in one turn, confirm in the next. The card is not a chat turn |
| 9 | Reserve "per item" | One reservation per take, under the id its child job will have, and one for the instrumentals under `{jobId}-instrumentals`, all written in one transaction with the one-active guard. Split items are 0 credits, so they have no reservation until step 8 | Today a reservation belongs to one job id, and a take settles its own. Keying each take's reservation by its child's id lets every child settle and refund through today's functions, unchanged. The instrumentals have no child, so the reconciler settles theirs with one new function |
| 10 | A failed or partial split, more parts than quoted, or solfège failing when only solfège was chosen: the job pauses (`needs_decision`) | In step 3 the job ends there. Nothing unsung is charged. Splits that worked stay in the Song, so the next quote doesn't split them again | Pausing, reviewing and re-quoting are step 6. Until then, ending with a refund is the safe side |
| 11 | "Every Sing All job sings on the RTX batch job" | True in production. A setting chooses the transport: the render job, or today's per-take dispatch for local runs and tests | The render job can't run on a laptop. The takes, credits and reconciler are the same either way |
| 12 | The switch "swaps the score's syllables and the player's takes" | Step 3 swaps the player's takes. The score preview is unchanged | The preview has no control today for which lyric line it shows. That is its own task |
| 13 | After "Sing it": "an email arrives when the job ends" | The card says the user can close the page and the result will be in the Song. No email is mentioned | Emails are step 5 |
| 14 | The quote lists and reserves the worst case for each staff ("2 or 3 parts", "up to N credits"), and lines not used are refunded | The quote is exact: the call has already answered shared or separate. No "up to", no "maybe" rows, no refund for unused lines. If a split still makes parts other than the rows quoted, the job ends and everything unsung is refunded | With the lane answer at the quote, there is no worst case left to reserve. A user is no longer told they lack credits for a job they can afford |
| 15 | The planner answers shared or separate at split time | The split is given the quote's answer and doesn't ask again. The planner's code takes the answer and fixes that question; its prompt and its other questions are unchanged. A split started in chat is unchanged | Asked twice, the two answers could differ, and the split would make parts the user wasn't quoted for |
| 16 | Not covered | The quote call has its own model setting. It defaults to the chat model, not the planner's model | Alan: try the normal model first. Moving it to the planner's model is one env line |
| 17 | After a split, each row's voice is picked again from the real part's notes | The voice quoted is the voice sung. After a split only the part id and the name are updated | Picking again needs a second call, and the user would hear a voice the card didn't show. The quote's range for a row that doesn't exist yet is an estimate; S1 prints how far it is from the real one |
| 18 | Not covered: the singing language | The chat model decides it first, as for a take. The "Sing All" button sends a visible chat message "Sing All"; the chat model resolves the language by its language rule against every enabled voicebank. With a voice for it, it calls a new tool `open_sing_all {language}`, which opens the card. With none, it replies as today: another score in a supported language, or solfège. If the user chooses solfège, it calls `open_sing_all {language: "en", solfegeOnly: true}`: the card quotes solfège only (Lyrics and Both disabled) and every take is English with the solfège pronunciation fix. A language it can't tell (Mandarin or Cantonese) it asks first. The quote call is given the language and doesn't ask it. The card's Language dropdown lists only the languages the enabled voicebanks sing (Alan, 2026-11-15) | Today the chat model decides the language before any take, and checks it against the voicebanks; a song no voice can sing gets the same answer in chat as today. Code alone can only tell Japanese, Thai and Chinese from the script. An English voicebank is always enabled |

## Data structure

```text
 users/{uid}
   NEW  activeSingAllJobId   str or absent     the one-at-a-time guard
     │
     ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ singAllJobs/{jobId}                    NEW   the process and the receipt     │
│                                                                              │
│  uid, sessionId, scoreId, title, kind "full"                                 │
│  status   running | completed | partial | failed | cancelled                 │
│  stage    reading | splitting | solfege | singing | done                     │
│  choices  {rows:[{rowId, include, voice, voicePicked}], language,            │
│            solfegeOnly, mode, verse, repeats}                                │
│  style    {note, controls: {rowId: {voiceColor, airiness, clarity, gender,   │
│            intensity}}}                                                      │
│  quote    {quoteId, total, choicesHash, scoreVersion}                        │
│  judgment {inputHash, roles, sounds, layouts, voices, model}                 │
│  rows[]   one per singing line (see below)                                   │
│  items[]  one per chargeable piece of work (see below)                       │
│  cancelRequested, endReason, notes[]                                         │
│  render   {state none|waiting|running|ended, queuedAt, startedAt, attempt}   │
│  createdAt, updatedAt, lastProgressAt, endedAt, finalScoreVersion            │
└───────┬───────────────────────────────────────────────▲──────────────────────┘
        │ items[].childJobId                            │ parentJobId, parentItemId
        ▼                                               │
┌──────────────────────────────────────┐   ┌────────────┴─────────────────────┐
│ jobs/{childJobId}        (existing)  │   │ credit_reservations/{childJobId} │
│  jobKind preprocess | synthesis      │   │ (existing) one per take item,    │
│  NEW parentJobId, parentItemId       │   │ + {jobId}-instrumentals          │
│  not the Song's currentJobId         │   │ NEW parentJobId                  │
└──────────────────────────────────────┘   └──────────────────────────────────┘

 sing_all_quotes/{quoteId}   NEW   uid, sessionId, scoreId, scoreVersion,
                                   choicesHash, rows, items, total, judgment,
                                   expiresAt
 sessions/{id}/singAll/judgment    NEW   the quote call's answers, kept per Song, the last 5 by inputHash
                                   (so switching the language back makes no call):
                                   inputHash   hash of what was sent (the language included), the prompt and the model
                                   layouts     {"{partId}|{staff}": shared | separate}
                                   voices      {rowId: voicebank id}
                                   roles       {partId: voice | instrument | unsure}
                                   sounds      {instrumentId: {bank, kind, program, evidence} | null}   null: none chosen
                                   model, createdAt
 sessions/{id}/singAll/style       NEW   the style call's answer, kept per Song:
                                   inputHash   hash of the note, the rows' voices, the prompt and the model
                                   controls    {rowId: {voiceColor, airiness, clarity, gender, intensity}}
                                   model, createdAt
 sessions/{id}/player/current      vocals[] gains `mode`; the key is (part, mode)
 sessions/{id}.history[i]          an entry may carry `singAllJobId`
```

### `rows[]`: one singing line

| Field | Type | Meaning |
|---|---|---|
| `rowId` | str | The lane key for a line that comes out of a split, e.g. `p2:staff1:V1:slot2`; else the part id |
| `sourcePartId` | str | The part in the score as uploaded |
| `partId` | str or null | The part that is sung. Null until the split makes it |
| `name` | str | Shown in the card. Replaced by the real part's name after a split |
| `split` | bool | True when the line comes out of a split in this job |
| `hasWords` | bool | False: it can only be sung in solfège |
| `note` | str or null | `no_words`, `likely_instrument`, `undecided`, `voice_out_of_range` |
| `voice` | str | Voicebank id |
| `voicePicked` | `recommended` or `user` | A voice the user picked is never replaced |

### `items[]`: one chargeable piece of work

| Field | Type | Meaning |
|---|---|---|
| `itemId` | str | `split:{sourcePartId}`, `take:{rowId}:{mode}`, `instrumental` |
| `type` | `split`, `take`, `instrumental` | Mix items arrive in step 4 |
| `rowId`, `mode` | str | For a take: the line, and `lyrics` or `solfege` |
| `childJobId` | str | Set at "Sing it" for every split and take. The child job is created later, under this id |
| `estimatedSeconds`, `unitSeconds` | number | As in the HLD. `unitSeconds` is 30 |
| `range` | null | Kept for the future preview (HLD §12) |
| `creditsReserved`, `creditsCharged` | int | Split items are 0 in step 3 |
| `status` | `queued`, `running`, `done`, `failed`, `stopped` | |
| `reason` | str or null | Why it failed or was stopped |
| `reservationId` | str | Instrumental only: `{jobId}-instrumentals`, its own reservation |

### Who writes what

| Record | Written by |
|---|---|
| `sing_all_quotes` | `POST /sing-all/quote` |
| `sessions/{id}/singAll/judgment` | `POST /sing-all/quote`, only when it makes the call |
| `singAllJobs`, `users.activeSingAllJobId`, the take reservations | `POST /sing-all/jobs` in one transaction; after that only the reconciler and Stop |
| `jobs/{childJobId}` | The reconciler creates it; the worker runs it as today |
| Player record | Each finished take, through today's hook |
| Song history | Two entries per job: one at "Sing it", one at the end |

## API

All need sign-in.

| Method and path | Purpose | Task |
|---|---|---|
| `GET /sing-all/active` | My running Sing All's progress, or none | S3 |
| `POST /sing-all/quote` | Rows, voices, options and price for a Song and a set of choices. 503 `quote_unavailable` when the quote call fails | S1 |
| `POST /sing-all/jobs` | "Sing it": start the job for a quote | S2 |
| `GET /sing-all/jobs/{id}` | One job's progress and receipt, for its owner | S3 |
| `POST /sessions/{id}/stop` (existing) | Also stops a Sing All running in that Song | S4 |
| `POST /internal/sing-all/reconcile` | Body `{jobId}`. Called by Cloud Tasks or a worker | S2 |
| `POST /internal/sing-all/sweep` | Called by Cloud Scheduler every 5 minutes | S4 |

`GET /sing-all/jobs` (the list) is left for step 5, with the job link.

## API contract (fixed 2026-11-17)

The page is built against these shapes. A field may be added later; none is renamed or removed without a spec change. Where a rule elsewhere in this spec says what an endpoint "returns", this section gives the exact shape.

Every endpoint needs sign-in (401 otherwise). An error is an HTTP status with the body `{"detail": {"code": str, …extra fields}}`. The Sing All endpoints use camelCase; `GET /songs/{id}/workspace` keeps its existing snake_case and only gains the fields named below.

### The chat reply that opens the card (S1 change 9, built)

```text
chat response   {type: "chat_text", message: str, singAll?: {language: str, solfegeOnly: bool}}
                # singAll present: the page opens the card in this reply's bubble and asks for a quote with
                # choices {language, solfegeOnly}. The card is not saved; after a reload it is gone until "Sing it".
```

### `POST /sing-all/quote`

```text
request   {songId: str,
           choices?: {                                   # every field optional; absent = the quick-mode default
             language?:    str,                          # default "en"; forced to "en" when solfegeOnly
             solfegeOnly?: bool,                         # default false; forces mode "solfege"
             mode?:        "lyrics" | "solfege" | "both",                 # default "lyrics"
             verse?:       str,                          # default the lowest of options.verses, else "1"
             repeats?:     bool,                         # default options.repeatsAvailable
             rows?:        [{rowId: str, include: bool, voice: str, voicePicked: "recommended" | "user"}],
                           # a row not listed keeps its default; a voice is kept only with voicePicked "user"
             styleNote?:   str}}                         # sent only by "Apply"; "" or absent = no style

200, a quote:
{
  quoteId:     str,
  blocked:     null | "free_plan" | "nothing_to_sing" | "not_enough_credits" | "style_unavailable",
  rows:        [Row],                                    # every row, ticked or not, top part first
  voices:      [{id: str, name: str}],                   # the enabled voicebanks that sing options.language;
                                                         # the only voices Customise offers
  options:     {language: str,
                languages: [{code: str, label: str | null}],   # every language an enabled voicebank sings;
                                                               # label from language_details, else null
                                                               # (the page names it with Intl.DisplayNames)
                solfegeOnly: bool, mode: "lyrics" | "solfege" | "both",
                verse: str, verses: [str],               # verses: the score's available verses, [] if none
                repeats: bool, repeatsAvailable: bool},  # repeatsAvailable false: sung in written order
  lines:       [Line],
  total:       int, balance: int,                        # credits
  notes:       [Note],
  style:       {note: str,
                controls: {rowId: {voiceColor: str | null, airiness: num, clarity: num, gender: num, intensity: num}},
                changes:  {rowId: {voiceColor?: str, airiness?: num, clarity?: num, gender?: num, intensity?: num}}},
                # controls and changes are {} with no note; changes holds only what differs from the
                # row's voice defaults, as differences (airiness +10 → 10), voiceColor as the colour's name
  choicesHash: str
}

200, no quote (no quoteId, nothing else):
{blocked: "already_running", activeJobId: str, activeSongId: str}       # link to the running job's Song
{blocked: "no_voice_for_language", language: str, supported: [str]}

Row  = {rowId: str, sourcePartId: str, partName: str,     # partName: the source part's name
        partId: str | null,                               # null for a line a split will make
        name: str,                                        # the line's own name, e.g. "Soprano", "Tenor · voice 2"
        split: bool, hasWords: bool,
        note: null | "no_words" | "likely_instrument" | "undecided" | "voice_out_of_range",
        include: bool, voice: str, voicePicked: "recommended" | "user",
        pitchRange: {lowest_note: str, highest_note: str} | {}}
Line = {kind: "singing", parts: int, takes: int, secondsEach: num, credits: int}
     | {kind: "preparing", credits: 0}                    # only when a split is needed
     | {kind: "instrumentals", credits: int}              # only when an instrumental item exists
     | {kind: "total", credits: int}
     | {kind: "balance", before: int, after: int}
Note = {code: "cannot_split_without_lyrics" | "cannot_split_evidence_too_large"
            | "no_sound_chosen" | "repeats_written_order",
        partId: str | null, partName: str | null}         # null for a note about the whole score
```

| Error | When |
|---|---|
| 404 `song_not_found` | No such Song, or not the caller's |
| 409 `score_unavailable` | The Song has no parsed score |
| 422 `invalid_mode`, `invalid_rows` | A malformed choice |
| 503 `quote_unavailable` | The quote call failed after its re-ask; the card offers "Try again" |

### `POST /sing-all/jobs` ("Sing it")

```text
request   {quoteId: str}
200       Progress                                       # the new job, status "running", stage "reading"
```

| Error (409) | When | The page |
|---|---|---|
| `quote_changed` | The quote is gone, expired, not the caller's, or no longer matches the Song | Asks for a new quote with the same choices and shows it: "The score changed, so here is the new quote." No new quote is sent in the error |
| `sing_all_already_running`, with `activeJobId`, `activeSongId` | A Sing All is running | Links to that Song |
| `not_enough_credits` | The balance changed since the quote | "Top up" |
| `free_plan`, `nothing_to_sing`, `style_unavailable` | As in the quote | As in the quote |
| any other code | Nothing was reserved or started | "Couldn't start. Try again." |

### `GET /sing-all/active` and `GET /sing-all/jobs/{jobId}`

```text
GET /sing-all/active         200 {job: Progress | null}  # the caller's running Sing All, from any Song
GET /sing-all/jobs/{jobId}   200 Progress                # any status; 404 job_not_found if missing or not the caller's

Progress = {
  jobId: str, songId: str, title: str,
  status: "running" | "completed" | "partial" | "failed" | "cancelled",
  stage:  "reading" | "splitting" | "solfege" | "singing" | "done",
  endReason: null | "score_replaced" | "quote_changed" | "split_failed" | "solfege_failed"
           | "nothing_to_sing" | "instrumentals_failed" | "stopped" | "stuck",
           # S3 maps the reconciler's own reasons onto these; the page treats any other value as a plain failure
  cancelRequested: bool,
  choices: {language: str, solfegeOnly: bool, mode: "lyrics" | "solfege" | "both", verse: str, repeats: bool},
  counts:  {splits: {done: int, total: int}, takes: {done: int, failed: int, total: int}},
           # done: made; failed: failed or stopped; total: every take item
  rows:    [{rowId: str, name: str, voice: str, voiceName: str, split: bool,
             takes: [{mode: "lyrics" | "solfege",
                      status: "queued" | "running" | "done" | "failed" | "stopped",
                      reason: str | null}]}],            # ticked rows only, in quote order
  credits: {reserved: int, charged: int, refunded: int}, # reserved: still held; the three add up to the quote total
  receipt: [{itemId: str, type: "take" | "split" | "instrumental",
             rowId: str | null, mode: "lyrics" | "solfege" | null, name: str | null,
             credits: int, outcome: "charged" | "refunded" | "included", reason: str | null}],
           # [] while running; every item once the job has ended ("included": a 0-credit split)
  notes:   [Note],                                       # as in the quote
  elapsedS: int, remainingS: int | null,                 # remainingS rough; null once ended
  render:  {state: "none" | "waiting" | "running" | "ended", placeInLine: int | null},
           # placeInLine: 1 = next to start; null unless waiting (filled by S5)
  createdAt: str, endedAt: str | null                    # ISO 8601
}
```

The page polls `GET /sing-all/active` while a job runs. When it answers `{job: null}` for a job the page was showing, the page reads `GET /sing-all/jobs/{jobId}` once for the ended card.

### `POST /sessions/{id}/stop` (S4 adds `singAll`)

```text
200 {result: "stopped" | "too_late" | "not_stoppable" | "nothing_running",
     jobs:   [{jobId, jobKind, result, refundedCredits}],   # chat jobs only; Sing All children are never listed
     singAll: null | {jobId: str, result: "stopping" | "too_late"}}
     # stopping: cancelRequested is set; finished takes are kept and charged, the rest refunded
     # too_late: every remaining take is past its stop point; the job completes
     # result counts singAll "stopping" as "stopped" and "too_late" as "too_late"
```

Replace on a Song with a running Sing All stops it the same way first; if `singAll.result` is `too_late`, the upload answers today's 409 `stop_too_late` and the score is not replaced.

### `GET /songs/{id}/workspace` (fields added)

```text
messages[i].singAllJobId?: str     # on the "Sing All started" and "Sing All finished" entries;
                                   # the page shows the card once, at the first entry with that id
player.vocals[i].mode: "lyrics" | "solfege"         # a track saved before step 3 reads "lyrics"
jobs: Sing All children (jobs with parentJobId) are left out; the card shows them
```

### Who builds what

| Part | Task |
|---|---|
| The chat reply, the quote's request, rows, lines, total, balance, blocked, style.controls | S1 (merged) |
| The quote's `voices`, `options.language`, `options.languages` labels, `options.verses`, `options.repeatsAvailable` (and the `repeats` default), structured `notes` with `no_sound_chosen` and `repeats_written_order`, `style.changes` always present, `activeSongId` | S3 backend |
| "Sing it" answering `Progress`; `activeSongId` on `sing_all_already_running` | S3 backend (S2 answers the job record until then) |
| `GET /sing-all/active`, `GET /sing-all/jobs/{jobId}`, `Progress`, the workspace fields | S3 backend |
| The stop response's `singAll`, children left out of `jobs`, Replace | S4 |
| `render.placeInLine` | S5 (S3 answers `null`) |


---

## B. Stop a split

### Parent

`feature/sing-all-prototype` after the merge above.

### Rule

```python
# worker, in the split job, just before the new score version is saved
if not job_store.pass_stop_point(job_id):
    discard the result; release the job as stopped; return      # the score is unchanged

# POST /sessions/{id}/stop and Replace
for job in active jobs of the Song:
    result = job_store.stop_job(job)        # today a split answers not_stoppable; now it is stopped like the rest
    if result == "stopped": add the history line for its kind
```

| When Stop arrives | Result | Score | Chat history |
|---|---|---|---|
| The split is planning or repairing | `stopped`. It ends at its next check, within about 30 s | Unchanged | "Stopped the split. Nothing was charged." |
| The split has passed its stop point | `too_late` | The new version is saved, as if Stop was never pressed | The split's normal reply |
| Replace during a split | The same stop, then the upload | The new score | The stop line, then the upload |

The planner checks for a stop before each LLM call and before each repair attempt, so a stopped split doesn't start another paid call. A call already running is paid for.

### Change

#### 1. Split job, `src/backend/orchestrator.py`

- `_run_preprocess_job`: call `pass_stop_point` just before `_activate_preprocessed_score` saves. If it returns False, save nothing and add no reply to the chat.
- Check `_is_user_stopped_job` before each planning call and each repair attempt, and end the run as stopped.
- A split result is also dropped when `job.scoreId != session.scoreId` (HLD §7.1).

#### 2. Stop, `src/backend/main.py`

- `_stop_song_work`: remove the `not_stoppable` branch for `preprocess`. Add the history key `job.stop_split_history` to `src/backend/messages/en.properties`.

#### 3. UI, `ui/src/MainApp.tsx`

- The Stop button is also shown while a split runs: remove `activeJobKind !== "preprocess"` from its condition. A stopped split shows "Stopped." like a stopped take.

### Scope

May change: the three files above, `src/backend/job_store.py` only if a split needs a field a take already has, tests. Must not change: the planner's prompts and rules, credits.

### Done when

- A test stops a split before its stop point: the job is `cancelled`, the session's score version is unchanged, no reply is added, and the stop line is in the history.
- A test stops it after the stop point: `too_late`, the new version is saved.
- A test replaces the score during a split: the split is stopped, the new score is in place, and the split's result is not saved onto it.
- The UI shows Stop during a split, and "Stopped." after it.

### Out of scope

Charging or refunding splits (step 8). Stopping inside one LLM call.

---

## S1. Quote and parts estimate

### Rule

`POST /sing-all/quote`, body `{songId, choices?}`. No score change, no credits touched. The first quote for a score makes one LLM call (rule J). Later quotes for the same score reuse its answer. With no `choices` it uses the quick-mode defaults.

```python
def quote(song, choices):
    if the user has a Sing All running: return blocked "already_running"     # before any LLM call
    score, summary = the Song's current score
    voicebanks = every enabled voicebank, each with its languages and language_details      # as the chat sees them
    language   = choices.language                 # from open_sing_all, or from the card's Language dropdown
    if choices.solfegeOnly: language = "en"; choices.mode = "solfege"       # the user chose solfège in chat
    facts      = quote_facts(score, summary, voicebanks)              # code only: rule R
    if no enabled voicebank can sing language:  return blocked "no_voice_for_language"    # a safety check only:
                                                    # the chat model checked it, and the dropdown lists only sung languages
    judgment   = quote_judgment(song, facts, language)                # the one LLM call: rule J
    rows     = facts.rows, keeping for each open staff the rows of judgment.layouts[staff]
    for row in the rows of an undecided part:
        row.note = {"voice": "no_words", "instrument": "likely_instrument", "unsure": "undecided"}[judgment.roles[part]]
    for row in rows:
        row.voice = choices.voice[row.rowId] or judgment.voices[row.rowId]
        row.note  = "voice_out_of_range" if the row's lowest..highest note is outside that voice's supported_range
    style   = style_judgment(song, choices.styleNote, rows) if choices.styleNote else None   # rule S
    items   = build_items(rows, choices)                              # rule I
    total   = sum(item.creditsReserved)
    blocked = first that applies:
        "free_plan"           users.billing.activePlanKey == "free"
        "nothing_to_sing"     no row is ticked or can be sung
        "not_enough_credits"  total > available credits
        (and, before any of these, "no_voice_for_language" above)
        "style_unavailable"   a note was sent and the style call failed
    save sing_all_quotes/{quoteId}, with the style  (expires like a take quote)
    return rows, voices, options, lines, total, balance, blocked, notes, style      # the API contract's shape
```

**Quick-mode defaults** (from `sing-all-quick-mode-proposal.md` §2): every row ticked, except the rows of a part the call judged an instrument or couldn't judge (`likely_instrument`, `undecided`); mode lyrics; verse 1, or the lowest verse the score has; with repeats, or written order when the score's repeats can't be followed; the recommended voice for each row.

**Rule R: the facts, in code.** One pass over the parts of the score. It finds the rows, the staffs whose number of lines is open, and the parts whose role is undecided.

```python
for part in summary.parts:
    if part is a split part (derived):       continue      # reached through its source part
    if part is hidden:                        continue
    kind = "voice"      if part has words or a declared voice sound, or the chat model ruled it not an instrument,
                        or it was split into lines to sing (change 7)
           "instrument" if it has a declared instrument and no words, or the chat model gave it a sound
           "undecided"  otherwise                          # no words, no declared instrument, no ruling
    if kind == "instrument":                               # the Instrumentals line
        waiting_for_sound += its instruments that the score gives no usable sound      # the call chooses one (rule J)
        continue
    if kind == "undecided":  undecided_parts += part       # the call says voice, instrument or unsure (rule J);
                                                           # its rows are found below like any other part's

    existing = the visible split parts already made from this part
    if existing and choices.verse == score.selected_verse_number:
        rows += [Row(rowId=p.id, partId=p.id, split=False) for p in existing]        # difference 3
    elif not needs_split(score, part):                     # the synthesiser's own test, difference 2
        rows += [Row(rowId=part.id, partId=part.id, split=False)]
    elif the score has no lyrics anywhere:
        notes += "can't be split yet"; no row              # HLD §5.1, a score with no lyrics
    else:
        for staff in the part's staffs:
            counts = sheet_lane_counts(score, part, staff)
            shared = max(counts.peak, counts.real_voices)
            if counts.most <= shared:                      # one reading: code knows the number of lines
                rows += lane_rows(build_sheet_skeleton(score, target=part), staff)
            else:                                          # two readings: the call decides (rule J)
                open_staffs += Staff(key      = "{part.id}|{staff}",
                                     evidence = the planner's lane-layout evidence for this staff,
                                     rows_if  = {layout: lane_rows(skeleton built with lane_layouts {staff: layout}, staff)
                                                 for layout in ("shared", "separate")})

lane_rows(skeleton, staff) = [Row(rowId=lane_key(lane), partId=None, split=True,
                                  name=lane_name(part, staff, rank))                  # HLD §5.1
                              for lane in the skeleton's lanes on that staff, top line first]
needs_split(score, part)   = part.multi_voice_part or (part.missing_lyric_voice_parts and score.has_lyric_sources)
```

| Part in the score | Rows | Who decides how many | Split in this job |
|---|---|---|---|
| One line, with words | 1 | Code | No |
| One line, no words, declared as a voice | 1, sung in solfège, note `no_words` | Code | No |
| One line, no words, no declared instrument | 1. Voice: ticked, sung in solfège, note `no_words`. Instrument: unticked, note `likely_instrument`. Unsure: unticked, note `undecided` | Code, and the call for its role | No |
| The same, with more than one line | Its rows as for any part, ticked or unticked as above | As above | Yes, if a row is ticked |
| No words, no declared instrument, already ruled on in chat | As a voice part, or as an instrument | Code | As above |
| One staff, two voices, one note each | 2 | Code | Yes |
| One staff, voice 1 with two-note chords, voice 2 sounding with it | 3 | Code | Yes |
| One staff, two voices that each divide in two, never at the same time | 2 if shared, 4 if separate | The call | Yes |
| Two staves, one line each | 2 | Code | Yes |
| Already split in chat, same verse | Its split parts | Code | No |
| Already split in chat, another verse chosen | As if not split | As above | Yes |
| Needs a split, and the score has no lyrics | None; the quote says why | Code | No |
| A staff the call must decide, with MusicXML over the planner's size limit | None; the quote says that part can't be split | Code | No |
| A declared instrument with no words | None; counted in the Instrumentals line | Code | No |
| The same, with no usable sound in the score | None; counted in the Instrumentals line with the sound the call chooses, or with none chosen | Code, and the call for its sound | No |

`hasWords` is true for a row that comes out of a split, and for an unsplit part with a lyric line that is not generated solfège.

**The notes of a row that doesn't exist yet.** The voice question needs each row's range. For an existing part it is the summary's `pitch_range`. For a row a split will make, code estimates its notes and runs the same range calculation on them:

```python
pool = the staff's sung notes of the row's voice       # separate layout, or a staff that isn't open
       the staff's sung notes of every voice           # shared layout
n    = the row's place, top first, among the rows that share that pool
at each onset: the row takes the n-th note from the top; with fewer than n notes, the lowest one
```

It is an estimate: the split may hand a few notes to another row. S1 prints, for the made-up test scores, how far the estimated range is from the range of the part the split really makes.

**Rule J: the one call.** It answers four kinds of question: for each undecided part, voice, instrument or unsure; for each instrument waiting for a sound, and each undecided part it answers instrument, a sound; for each open staff, shared or separate; and for each row, which voice for the given language. It never asks the language: the chat model has decided it (difference 18), or the user picked it in the dropdown.

```python
def quote_judgment(song, facts):
    request = {
      "parts":      [{key, the part's entries in score_summary.parts[] as the chat model sees them,
                      other_parts: [{part_name, kind}]}     for each undecided part],
      "instruments": [{key, part_name, route melodic|percussion, reason,
                      the instrument's factual fields as the chat model sees them}
                                                            for each instrument waiting for a sound],
      "staffs":     [{key, part_name, staff_peak, lanes_if_shared, lanes_if_separate, voices,
                      score_xml}                            for each open staff],      # what the planner sees
      "rows":       [{key, staff, layout, part_name, row_name,
                      lowest_note, highest_note, tessitura_low_note, tessitura_high_note}
                                                            for each row, and for an open staff
                                                            its rows under both layouts],
      "language":   the language,                                   # always given
      "voicebanks": [{id, name, languages, language_details, supported_voice_types,
                      supported_gender_presentations, supported_range, optimal_range,
                      selection_priority}                   for every enabled voicebank],
    }
    # each voice must be able to sing the language
    # an undecided part's staffs and rows are asked like any other part's, whatever its role turns out to be,
    # so ticking it later under Customise needs no new call
    drop every question with one legal answer       # a given language that one voicebank sings: code gives it to every row.
    a quote where only the language changed asks only the voices; the roles, sounds and layouts come from
        the saved answer, so changing the language can't change them
                                                    # A role or sound question always has several, so it is never dropped
    if nothing is left to ask: return the code's answers

    key   = hash(request, the prompt text, the model)
    saved = sessions/{id}/singAll/judgment
    if saved.inputHash == key: return saved         # no call

    reply  = llm(role SING_ALL_QUOTE, prompt, request, quote_reply_schema(request))   # below
             # the transport retries are inside, as for every LLM call (post_with_retry)
    errors = check(reply)     # every part has voice|instrument|unsure; every staff has shared|separate;
                              # every row's voice is a listed voicebank that can sing the language
                              # (resolve_synthesis_language, as for a take);
                              # every listed instrument and every part
                              # answered instrument has a sound or null, and no other part has one; each sound
                              # passes the check the chat's sounds get for its route; no other key
    if errors:
        reply  = llm(…, request + "Your previous answer was invalid: {errors}. Answer every key with one listed id.")
        errors = check(reply)                       # once, as the planner does
    if only sounds still fail: those sounds = null          # included with no instrument chosen (difference 6)
    elif errors or the call raised: return 503 quote_unavailable
    keep the voices of the rows under each staff's chosen layout; save as sessions/{id}/singAll/judgment
```

**The reply schema says what each answer may be**, so the prompt doesn't spell out the reply. It is built in code for each request, with the request's short keys, and sent as the call's response schema, as for the style call:

```python
def quote_reply_schema(request):
    return {"type": "OBJECT", "required": [the four keys present], "properties": {
      "roles":   {each part key: {"type": "STRING", "enum": ["voice", "instrument", "unsure"]}},
      "sounds":  {each instrument key and each part key: {"type": "OBJECT", "nullable": True, "properties": {
                    "bank":     {"type": "INTEGER", "enum": [FLUIDR3_MELODIC_BANK] or [FLUIDR3_PERCUSSION_BANK]},
                    "kind":     {"type": "STRING",  "enum": ["melodic"] or ["percussion_kit"]},
                    "program":  melodic: {"type": "INTEGER", "minimum": 0, "maximum": 127}
                                percussion: {"type": "INTEGER", "enum": FLUIDR3_PERCUSSION_PROGRAMS},
                    "evidence": {"type": "ARRAY", "items": {"type": "STRING"}}}}},
                 # by the instrument's route; the constants are the ones _valid_preset_for_instrument
                 # in src/musicxml/instrument_programs.py checks, so the rule exists once
      "layouts": {each staff key: {"type": "STRING", "enum": ["shared", "separate"]}},
      "voices":  {each row key: {"type": "STRING", "enum": the ids of the voicebanks that sing the language}},
    }}
    # every listed part, staff and row key is required; a sound is required for each listed instrument.
    # What a schema can't say is checked in code: a part's sound only when its role is instrument.
```

| What changes | A new call? |
|---|---|
| Mode, verse, repeats, ticks, or a voice picked by the user | No. None of them is in the request |
| The score: an upload, an edit, a split or added solfège in chat | Yes, on the next quote |
| The chat model rules on an undecided part or chooses a sound | Yes, on the next quote, without that question |
| "Sing it" saves the call's roles and sounds (S2) | Yes, on the next quote, without those questions |
| The language, from the Language dropdown | Yes, unless that language's answer is saved; it asks only the voices |
| The enabled voicebanks, the prompt or the model | Yes, on the next quote |
| "Sing it" | Never. See S2 |
| Anything during the job | Never. The job carries the answer |

**The model.** A new role, `SING_ALL_QUOTE`, with its own settings: `GEMINI_SING_ALL_QUOTE_MODEL`, `GEMINI_SING_ALL_QUOTE_THINKING_LEVEL`, `GEMINI_SING_ALL_QUOTE_TIMEOUT_SECONDS`. Each defaults to the chat model's value (`GEMINI_DEFAULT_*`), so the first trial runs on the normal model and a change to the planner's model is one env line. With the OpenAI provider it uses the default role's settings. The call sends no chat history and doesn't count against a chat turn's call limit.

**The prompt: Sing All's task, then the current prompts as references** (Alan, 2026-11-15). The new text names the sections of the existing prompts to follow and says only where Sing All differs. It quotes no rule, so a later change to a rule reaches Sing All with no second edit.

```python
# src/backend/sing_all_prompts.py
quote_prompt = "\n\n".join([
    read src/backend/config/system_prompt_sing_all_quote.txt,             # new: the task and the differences
    "The chat assistant's instructions",
    the chat prompt's fixed text, as _load_system_prompt() gives the default model,
        with its settings filled in; no score context and no tool list,   # unchanged file
    "The part-splitting planner's instructions",
    read src/backend/config/system_prompt_planner_v2.txt,                 # unchanged file
])
```

The new file, `src/backend/config/system_prompt_sing_all_quote.txt`. Draft for Alan's review:

```text
You prepare a quote for singing every vocal part of a score. The app has already counted the notes.
You are not the chat assistant or the part-splitting planner, and you call no tools. Their
instructions follow this task: first the chat assistant's, then the part-splitting planner's. Use
only the sections named below, with the differences listed. Ignore everything else in them,
including their reply formats.

Reply with JSON only, as the reply schema describes. Answer every key it lists; a sound only where
Sound asks for one.

Role, for each entry in "parts": judge it as the chat assistant's "Which parts a request covers"
rule judges a part. Difference: answer with one word instead of including or leaving out the part:
voice where the chat assistant would include it, instrument where it would leave it out as an
instrument, and unsure where it would leave it out because it cannot judge it.

Sound, for each entry in "instruments" and each part you answered instrument: choose it as the
chat assistant's "Instrument playback presets for MIDI playback" section chooses a playback_preset.
Differences: put the bank, kind, program and evidence in "sounds" instead of a tool call, without
soundfont_id or source; answer null instead of guessing when the facts give no basis for a sound;
and give no sound to a part you answered voice or unsure, instead of setting role not_instrumental.

Layout, for each entry in "staffs": answer as the part-splitting planner's "lane_layout" kind says.
Difference: none.

Voice, for each entry in "rows": choose it as the chat assistant's "Voicebank selection" section
chooses a voicebank, for the "language" in the request. Differences: choose for every row instead of for one
part, reading each row's part_name and row_name as the requested role and gender, and its note
fields as the part's pitch_range; skip the steps about a user-selected or UI-selected voicebank and
confirmed_voicebank_override, because none is selected; give the closest voicebank without stating
a limitation, instead of stating it before synthesis. Answer every row listed, including both
layouts of a staff in "staffs" and the rows of every part in "parts".
```

Two rows may get the same voice. A voice the user picks under Customise is kept as it is; Customise lists only voices that can sing the language.

**The Language dropdown and a language change.**

```python
dropdown = sorted union of every enabled voicebank's languages            # an empty list counts as "en", as today
           labelled from their language_details                           # no world list of languages
the dropdown starts at the language open_sing_all gave
solfège only (the user chose solfège in chat): the dropdown shows English and is disabled
user picks a language:
    new quote with choices.language = it
    a voice the user picked under Customise that can sing it:  kept
    every other row's voice:                                   asked again, in one call, voices only
    that language's answer already saved:                      no call
```

| The user changes the language | Rows |
|---|---|
| To one the quoted voices can all sing | The call is asked the voices again for that language, so a better-fitting voice may replace one; a voice the user picked is kept |
| To one some quoted voices can't sing | Those rows get a voice that can, from the same call |
| Back to a language quoted before | The saved answer comes back; no call |
| To one no enabled voicebank sings | Not possible: the dropdown lists only languages a voicebank sings |

**Rule S: the style call.** It runs only when "Apply" sends a note. Customise's live quotes (each tick, voice or mode change) don't send the note, so typing makes no call; the price never depends on the style.

```python
def style_judgment(song, note, rows):
    request = {
      "note":  note,
      "rows":  [{key, part_name, row_name, voicebank_id,
                 voice_colors: that voicebank's colour ids and names, default_voice_color,
                 defaults: that voicebank's synthesis_control_defaults}     for each row, ticked or not],
    # every row is asked, so ticking one after an Apply needs no new call
    }
    key   = hash(request, the prompt text, the model)
    saved = sessions/{id}/singAll/style
    if saved.inputHash == key: return saved          # the same note and voices: no call
    schema = style_reply_schema(rows)                # below: each control's type, range and meaning
    reply  = llm(role SING_ALL_QUOTE, style prompt, request, schema)
    errors = check(reply)     # the same rules as the schema, checked in code too, since a model may not keep to it
    if errors: reply = llm(…, request + "Your previous answer was invalid: {errors}."); errors = check(reply)   # once
    if errors or the call raised: return failed     # the quote is blocked "style_unavailable"
    save as sessions/{id}/singAll/style
```

| What changes after an Apply | A new style call? |
|---|---|
| Ticks, mode, verse, repeats | No |
| The note | Yes, on the next Apply |
| A row's voice | Yes, on the next Apply: colours and defaults belong to the voice |
| The note is cleared | No call; every take uses its voice's defaults |

**The reply schema says what each control is**, so the prompt doesn't list them. It is built in code for each request and sent as the call's response schema, which the Gemini client already passes through (`responseSchema`; the OpenAI client converts it). Each control's range and description are read from the `synthesize` tool's own schema in `src/mcp/tools.py`, so they exist once:

```python
def style_reply_schema(rows):
    control = lambda name: {type, minimum, maximum, description} of the synthesize tool's `name` property
    return {"type": "OBJECT", "required": ["rows"], "properties": {"rows": {
        "type": "OBJECT", "required": [every row key], "properties": {
            row key: {"type": "OBJECT",
                      "required": ["voiceColor", "airiness", "clarity", "gender", "intensity"],
                      "properties": {
                          "voiceColor": {"type": "STRING", "nullable": True,
                                         "enum": that row's voicebank colour ids,           # absent: always null
                                         "description": the tool's voice_color description},
                          "airiness":  control("airiness"),     # -100..100, BREC
                          "clarity":   control("clarity"),      # 0..200, VOIC
                          "gender":    control("gender"),       # -100..100, GENC
                          "intensity": control("intensity"),    # 0..1, TENC multiplier
                      }}
            for each row}}}}
```

Intensity is added for Sing All (Alan, 2026-11-15). The chat prompt has no rule for it, so the style prompt says how to set it; its start is the voicebank's `synthesis_control_defaults.intensity`. Articulation stays at its default: it has no voicebank default and no rule.

The summary shows, per ticked row, only the controls the note changed, each as the difference from that voice's default ("airiness +10", "gender −10", "intensity +0.2"), and the colour's name when one was chosen.

**The style prompt**, built the same way: `system_prompt_sing_all_style.txt`, then the chat assistant's instructions only.

The new file, `src/backend/config/system_prompt_sing_all_style.txt`. Draft for Alan's review:

```text
You set the voice controls for each row of a song from the user's style note. You are not the chat
assistant and you call no tools. The chat assistant's instructions follow this task. Use only the
lines named below, with the differences listed, and ignore everything else in them.

Reply with JSON only:
{"rows": {row key: {"voiceColor", "airiness", "clarity", "gender", "intensity"}}}, as the reply
schema describes each one.

Set each row as the chat assistant's lines on voice_color and on airiness, clarity and gender
(after "Voicebank selection") set a take's style. Differences: the user's style request is "note",
and it covers every row instead of one take, so a note that names a part ("darker bass") changes
only that part's rows; give every control for every row as an absolute value instead of omitting
one to keep its default; set intensity the same way as airiness, clarity and gender, starting from
the row's default; and for a row the note doesn't concern, keep its default_voice_color and
defaults instead of choosing a colour from the song. Each row gives its voicebank, voice_colors,
default_voice_color and defaults.
```

**Rule I: the items and the price.**

```python
sing_seconds = selected_score_duration(summary, expand_repeats=choices.repeats)     # the same for every row
for row in ticked rows:
    modes = ["solfege"]            if choices.mode == "solfege" or not row.hasWords
            ["lyrics"]             if choices.mode == "lyrics"
            ["lyrics", "solfege"]  if choices.mode == "both"
    for mode in modes:
        items += Take(row, mode, credits = ceil(sing_seconds / 30))                  # today's take rate
for each source part with a ticked split row:
    items += Split(part, credits = 0)                                                # "included" until step 8
if the score has instrument parts, and this score's instrumentals are unpaid:        # every part not sung counts,
    items += Instrumental(credits = ceil(sing_seconds / 120))                       # today's instrumentals rate; with or without a sound (difference 6)
                                                                                    # difference 5
```

The numbers come from today's functions (`estimate_synthesis_credits`, `selected_score_duration`, `score_has_instrumental_parts`, the instrumental charge record), not from new arithmetic. `score_has_instrumental_parts` runs on a copy of the summary with the call's answers applied, as "Sing it" will save them (S2).

| Quote line | Shown when | Example |
|---|---|---|
| Singing N parts (about m:ss each) | Always | "Singing 4 parts (about 0:59 each) · 8 cr" |
| Preparing the parts | A split is needed | "Preparing the parts · included" |
| Instrumentals | An instrumental item exists | "Instrumentals · 1 cr" (a song up to 2:00) |
| Total | Always. It is exact | "9 cr" |
| Balance | Always | "42 → 33" |

| Case | The card |
|---|---|
| The quote is being made | "Reading the score…" until the answer arrives |
| The language | In the summary line, e.g. "4 vocal parts · English · verse 1 of 2"; under Customise, the Language dropdown |
| Solfège only (the user chose solfège in chat) | Sing in: Solfège, with Lyrics and Both disabled; Language: English, disabled. Every take is English with the solfège pronunciation fix |
| `no_voice_for_language` (a safety check; shouldn't happen) | "This language can't be sung now. Ask in the chat to start Sing All again." No quote |
| The quote call failed | "The quote couldn't be made." with "Try again" |
| Free plan | Button "Upgrade to sing" |
| A style note applied | Under the summary line: the note, then one line per ticked row with its voice and the controls the note changed, e.g. "Alto · Voice B · airiness +10 · gender −10" |
| The style couldn't be applied | "The style couldn't be applied." with "Try again", which applies it again. No "Sing it" until it applies or the note is cleared |
| Not enough credits | Total in red, "Top up" in place of "Sing it" |
| A row with no words | "Bass has no words, so it's sung in solfège (do-re-mi)" |
| A part the call judged an instrument | Listed under Customise, unticked: "looks like an instrument". Unticked, it is played in the instrumentals; ticking it sings it in solfège instead |
| A part the call couldn't judge | Listed under Customise, unticked: "not sure this is a voice part". Unticked, it is a MIDI track with no instrument chosen; ticking it sings it in solfège |
| An instrument with no sound chosen | "Piano 2 has no instrument yet: choose one in the player" |
| No lyrics and a part that needs a split | "This score has no lyrics, so its shared-staff parts can't be split yet"; the other parts can still be sung |
| A voice that can't reach the notes | The row says the voice may be out of range |
| Repeats can't be followed | Sung in written order, with one line saying so |
| A Sing All is already running | No "Sing it"; a link to the running job's Song. No quote is made |

### Change

#### 1. Facts, new file `src/api/sing_all_estimate.py`

- `quote_facts(score, summary, voicebanks) -> rows, open_staffs, notes`. `build_sheet_skeleton` already takes `lane_layouts`, so the rows of both layouts come from the planner's own code. Also uses `sheet_lane_counts`, the planner's lane-layout evidence and the existing-lane lookup in `src/api/preprocess_sheet.py`, `trim_musicxml` in `src/api/preprocess_evidence.py`, and the two signals `synthesize_preflight_action_required` reads in `src/api/voice_parts.py`. `needs_split` is moved into one small function that the preflight check also calls, so the rule exists once.
- The lane key and lane name follow HLD §5.1. The estimated notes of a row use the parser's own `pitch_range` calculation.

#### 2. The call, new file `src/backend/sing_all_judgment.py`, and `src/backend/orchestrator.py`

- `build_request`, `check_reply`, `input_hash`: pure functions. Short keys (`s1`, `r1`) in the request, mapped back in code, as the planner does.
- `check_reply` checks each sound with the per-route check the chat's sounds get (`_normalise_preset` and `_valid_preset_for_instrument` in `src/musicxml/instrument_programs.py`, used as they are). A sound that still fails after the re-ask becomes null; it never fails the quote.
- `Orchestrator._call_sing_all_quote(request)`: one request, the quote prompt from `src/backend/sing_all_prompts.py`, no history, `quote_reply_schema(request)` as its response schema, not counted against the turn limit. It mirrors `_call_preprocess_chooser`.
- The invalid-answer re-ask is a second copy of the planner's (`_ask` in `src/api/preprocess_chooser.py`). A third use would mean sharing it.
- A log line per call: time taken, number of staffs and rows, model. No titles, part names or lyrics.

#### 3. Model role, `src/backend/llm_client.py`, `src/backend/llm_gemini.py`, `src/backend/llm_openai.py`, `src/backend/config/__init__.py`

- `LlmRole.SING_ALL_QUOTE` and its three settings, with the defaults above.

#### 4. Price, new file `src/backend/sing_all.py`

- `build_items`, `quote_lines`, `choices_hash`. Pure functions: no Firestore, no network. `choices_hash` covers the note and the style's controls, so "Sing it" sings exactly the values shown.
- `style_judgment` and its `check`, in `src/backend/sing_all_judgment.py`, through `Orchestrator._call_sing_all_quote` with the style prompt. The re-ask is the same helper as rule J's.

#### 5. Store, new file `src/backend/sing_all_store.py`

- `sing_all_quotes`: save and read. `sessions/{id}/singAll/judgment` and `sessions/{id}/singAll/style`: save and read. `singAllJobs` arrives in S2.

#### 6. Endpoint, `src/backend/main.py`

- `POST /sing-all/quote`. Checks that the Song is the caller's.

#### 7. Chat: a part split to sing is a voice part, `src/musicxml/instrument_programs.py` and `src/backend/orchestrator.py`

Today, when a part with no words and no declared instrument is split in chat, the lines made from it are voice parts, but the part itself stays undecided. At the next chat take the chat model is asked about it again, and an "instrument" answer would play its notes under its own sung lines. Splitting a part is done only to sing its lines, so code settles it (Alan, 2026-11-15; built with this spec, not as a separate fix on main):

```python
def settle_split_sources(summary, voice_part_transforms):          # new, pure
    sources = {lane.source.part_index for each transform in voice_part_transforms
               if its appended part is a visible part of the score}
    for part in summary.parts with part_index in sources:
        for instrument in part.instruments if instrument.instrumental_candidate:   # no words, no declared instrument
            instrument.instrumental_role = "not_instrumental"
            instrument.program_source    = "split_for_singing"
            remove its id from unresolved_score_instrument_ids and unresolved_reasons
```

| The part | Before the change | After |
|---|---|---|
| No words, no declared instrument, split in chat | Asked about at the next chat take; may be played | A voice part; not asked about, never played |
| The same, given a sound by the chat model before the split | Played under its own sung lines | A voice part; not played |
| A declared instrument, split | Unchanged | Unchanged: the rule covers only parts with no declared instrument |
| Not split | Unchanged | Unchanged |

- It is called wherever the score summary is rebuilt while the score keeps its split records: after a split's parts are added, and after a reparse (`_carry_preprocess_active_metadata`). The split records are the planner's own (`voice_part_transforms`, read as `_existing_lane_slots` in `src/api/preprocess_sheet.py` reads them).
- `apply_llm_program_assignments` then rejects a sound sent for that part, as for any part that isn't waiting.

#### 8. An instrument with no sound chosen, `src/musicxml/instrument_programs.py`, `src/musicxml/performance_midi.py`

Today only a drum part whose score gives no drum sounds can be a track with nothing chosen (`user_assignable`: muted, "Choose a kit" in the player). This widens it to any instrument, melodic or drum, for Sing All:

```python
apply_llm_program_assignments(summary, entries, allow_user_choice=False)       # new keyword, default unchanged
    entry {role: "instrumental", playback_preset: null}:
        allowed only with allow_user_choice=True                               # Sing All's save; chat tools never pass it
        instrument.user_assignable = True; program_source = "user_assignable"
        instrument.eligible_for_instrumental_midi = True
instrumental_programs_by_part reads user_assignable from the summary as well as from the score file
build_instrumental_performance_midis: a user_assignable part keeps its own percussion flag
    (today it is always set to percussion), and its diagnostic says "Choose an instrument"
    for a melodic part, "Choose a kit" for a drum part
```

- Chat is unchanged: its tools call the function without the keyword, so a null sound from the chat model is still refused.

#### 9. Chat: the language first, then the card, `src/backend/config/system_prompt.txt`, `src/mcp/tools.py`, `src/backend/orchestrator.py`

The "Sing All" button sends the visible chat message "Sing All". A typed request to sing every part, or more than one part, works the same way; the chat line that answered "one part at a time" is removed (Alan, 2026-11-15). The chat model decides the language before the card opens, as it does before a take (Alan, 2026-11-15).

```python
tool open_sing_all {language, solfegeOnly = false}      # listed only when the user's Song has a score
handler (code only, no LLM):
    language = normalize_language_code(args.language)                 # today's function in language_selection.py
    supported = the union of every enabled voicebank's languages      # an empty list counts as "en", as today
    first that applies, each returned to the chat model as action_required with its reason, no card:
        a Sing All already running for the user  → reason "sing_all_already_running"
        language missing or not a language code  → reason "language_required"
        language not in supported                → reason "no_voicebank_for_language",
                                                   with {language, supported}           # the safeguard
        solfegeOnly and language != "en"         → reason "solfege_only_needs_en"
    else: the reply carries singAll {language, solfegeOnly}; the page opens the card in that bubble
```

The safeguard is for a language the chat model gives that no enabled voicebank sings, even though its rule says to check first. The result goes back to the chat model through the existing follow-up path, as an unsupported language does before a take, so the chat model explains it and offers another score or solfège. The backend writes no user-facing text.

New lines in `system_prompt.txt`, a section of its own that refers to the existing rules. Draft for Alan's review:

```text
- Sing All (only when the open_sing_all tool is listed):
  - When the user asks to sing every part of the score ("Sing All"), or more than one part, resolve the
    singing language as "Language selection for synthesis" says, checking it against every available
    voicebank instead of one selected voicebank. Don't pass which parts were asked for: the card lists
    every part, and the user ticks or unticks them there.
  - If a voicebank can sing it, call open_sing_all with that language. Do not prepare a quote: the card
    does that.
  - If none can, return no tool call; explain it and offer a score in a supported language or solfège,
    as for a single take. If the user chooses solfège, call open_sing_all with language "en" and
    solfegeOnly true.
  - If the language can't be resolved (for example Mandarin or Cantonese), ask the user first.
```

- The chat model's turn and its follow-up count against the turn's call limit as usual. Sing All's quote call is not part of the turn.

### Scope

May change: the files above, `src/api/voice_parts.py` only to share `needs_split`, `src/musicxml/instrument_programs.py` only for change 7, tests. Must not change: `system_prompt.txt` except change 9's new Sing All section, `system_prompt_planner_v2.txt` and every other existing prompt; `credits.py`; the take quote path (`_quote_synthesis`). The two new prompt files hold only Sing All's own text and are used only with Alan's OK on their wording; the prompts they refer to are sent as they are, unchanged.

### Done when

- Tests for rule R: one test per row of its table, on made-up scores.
- Tests for the prompts: every section the new files name ("Which parts a request covers", "Instrument playback presets for MIDI playback", "Voicebank selection", "lane_layout", the voice_color lines) is found in the reference text it names, so a renamed heading fails a test; the built prompt is the new text followed by the references, unchanged; `system_prompt.txt` and `system_prompt_planner_v2.txt` are unchanged.
- Tests for rule J, with a stubbed LLM:
  - The request for a score with one open staff lists that staff once and its rows under both layouts; a score with no open staff sends no `staffs`.
  - A part with no words and no declared instrument is listed once under `parts`, with its rows under `rows`. Answer voice: its row is ticked, with `no_words`. Answer instrument or unsure: its row is unticked, with its note. Ticking that row afterwards makes no call and uses the voice already answered.
  - A part the chat model has ruled on (a sound, or not an instrument) is not listed under `parts`.
  - A declared instrument with no program is listed once under `instruments`, and its sound is in the answer. A part answered instrument has a sound; one answered voice or unsure has none.
  - The reply schema lists every key of the request, with roles and layouts as enums, each row's voices limited to the voicebanks that sing the language, and each sound's bank, kind and program limited by its route with the constants `_valid_preset_for_instrument` checks.
  - A missing sound, a sound for a part answered voice or unsure, a melodic sound on a percussion route and a program outside 0..127 each cause one re-ask. A sound still bad after it becomes null and the quote is made; a null answer is accepted.
  - An unknown voicebank id, a missing row, a layout other than shared or separate, and a role other than the three each cause one re-ask; a second bad answer gives 503 `quote_unavailable`.
  - A second quote, a quote after each Customise change, and "Sing it" make no call; a quote after the score changes makes one.
  - A given language that one voicebank sings, no open staff, no undecided part and no instrument waiting for a sound: no call.
  - `open_sing_all`'s handler, one test per reason: already running, no language or an invalid code, a language no enabled voicebank sings (the result names the language and the supported list, goes back to the chat model, and opens no card), solfège only with a language other than en; and a supported language opens the card with that language.
  - Language: the request always carries the given language and never asks it; the reply's voices must sing it (a voice that can't causes one re-ask); `solfegeOnly` gives language en, mode solfège only, and the take requests carry `require_solfege_lyrics` and `solfege_pronunciation_patch`; a language no enabled voicebank sings gives `no_voice_for_language` with no call; changing the language asks only the voices and keeps a voice the user picked that can sing it; switching back makes no call; the dropdown is the union of the enabled voicebanks' languages, with "en" for an empty list.
  - A voice picked by the user is kept; an answer's voice outside the row's range sets the row's note.
- Tests for rule I: lyrics, solfège and both; a row without words in each mode; instrumentals already paid for; an instrument whose sound the call chose (the item is there); a part answered instrument and unticked (the item is there); a part answered unsure and left unticked (not sung, the item is there); an instrument with a null sound (the item is there).
- An endpoint test for each `blocked` value, for `already_running` making no call.
- Tests for rule S, with a stubbed LLM: a quote without a note makes no style call; a note makes one, and the same note and voices again make none; a changed note or a changed voice makes one; the reply schema lists each row with its voicebank's colours and each control's range and description from the synthesize tool's schema; a colour not in the row's voicebank, a value out of range (intensity included) and a missing row each cause one re-ask, and a second bad answer gives `style_unavailable`; a cleared note uses the defaults; the summary values are the differences from each voice's defaults.
- In `tests/test_llm_gemini.py` and `tests/test_llm_openai.py`: the new role reads its own settings and falls back to the default role's.
- The test log prints the estimated range beside the real range for each made-up split score.

### Out of scope

The card (S3). Reserving anything (S2). Changing a sound in chat: the player's instrument dropdown does it, as today.

---

## S2. Parent job, credits and the reconciler

### Rule

**"Sing it":** `POST /sing-all/jobs`, body `{quoteId}`. It never calls the LLM.

```python
quote = sing_all_quotes[quoteId]                 # must be the caller's and not expired
if input_hash(the Song now) != quote.judgment.inputHash:
    return 409 quote_changed                     # the score or the voices changed: the page asks for a new quote
fresh = rows and items from the score now, quote.choices and quote.judgment
if fresh rows, items or total differ from quote: return 409 quote_changed     # difference 8; the page asks again
if fresh.blocked:                                return 409 + the reason
job = new parent, carrying quote.judgment; every split and take item gets its childJobId now

in ONE transaction:                              # difference 9
    if users.activeSingAllJobId points at a job that hasn't ended: return 409 sing_all_already_running
    check the total against the available credits, once
    write one reservation per take item, under its childJobId
    write the instrumentals' reservation, under {jobId}-instrumentals, if there is an instrumental item;
        it marks this score's instrumental charge pending for that id, as a take with instrumentals does today
    write singAllJobs/{jobId}  (status running, stage reading)
    set users.activeSingAllJobId
add the history entry {role assistant, singAllJobId, content "Sing All started: …"}
enqueue reconcile(jobId)
return progress(job)                             # the API contract's Progress (S3)
```

Every side effect after this is safe to repeat, because each child's id is fixed at "Sing it": creating a child that exists does nothing, dispatching it twice is one task, and releasing a reservation twice is a no-op.

**The reconciler.** `reconcile(jobId)` reads the parent and its children, decides in a pure function, writes the parent in a transaction, then carries out the actions. It runs at "Sing it", when a child ends, after Stop, and from the sweeper. It never calls the quote's LLM: every answer it needs is in `job.judgment`.

```python
def reconcile(job):
    if job.status != "running": return
    if job.cancelRequested:     return wind_down(job)          # S4
    match job.stage:
        case "reading":
            if session.scoreId != job.scoreId:            end("failed", "score_replaced")
            if choices.verse != the loaded verse:         reparse the score for that verse     # a new version
            if rows now (rule R with job.judgment) != rows quoted:        
                                                          end("failed", "quote_changed")
            save the call's roles and sounds into the score summary             # below; after the rows check,
                                                                                # because it changes what rule R reads
            stage = "splitting"
        case "splitting":                                  # one child at a time, on the version chain
            child = the running split child
            if child is running: return
            if child ended:
                ok = child completed, and no review required
                if not ok:                                 end("failed", "split_failed")           # difference 10
                map its new parts to rows by lane key      # HLD §5.1
                a new part with no row, or a row with no new part:
                                                           end("failed", "quote_changed")
                each mapped row: partId, name              # the voice stays as quoted, difference 17
                split item: done, 0 credits
            next = the next source part that needs a split
            if next:
                start its split child, with job.judgment.layouts for its staffs       # difference 15
                return
            stage = "solfege"
        case "solfege":
            if no take is in solfège mode: stage = "singing"
            else:
                add solfège lines to those rows' parts in one call      # deterministic, about 1 s, a new version
                a part that couldn't take a line: its solfège take is failed and released, with a note
                stage = "singing"
            if no take item is left to sing:               end("failed", "nothing_to_sing")
        case "singing":
            if nothing started yet:
                finalScoreVersion = the Song's version now
                prepare the instrumental files once        # today's function; a failure ends the job, all refunded
                start one child per take item              # transport: difference 11
            for each child that ended: item = done (credits charged) | failed | stopped
            if the instrumental item is reserved and a take is done:
                settle_instrumental_reservation(uid, "{jobId}-instrumentals")    # charged once, the score marked paid
            if every child ended:
                status = completed (all done) | partial (some done) | failed (none done)
                stage = "done"; clear users.activeSingAllJobId
                add the history entry "Sing All finished: …"
```

`end(status, reason)` releases every reservation not yet settled, the instrumentals' included, marks the remaining items, writes the end entry in the history and clears the guard. So the instrumentals are refunded only when no take was made.

| After a split | What happens |
|---|---|
| The parts made are the rows quoted for that part | Continue. Each row takes the real part's id and name and keeps its quoted voice |
| A lane failed, or review is required | The job ends `failed`, reason `split_failed`. The splits that worked stay in the Song |
| The parts made are not the rows quoted: more, fewer or different lane keys | The job ends `failed`, reason `quote_changed`. This should not happen, because the split is given the quote's layout; the check is there so a wrong count is never sung and charged |
| Names differ | No effect; rows match by lane key and take the real names |

**The split takes the quote's layout.** The quote already answered shared or separate for each open staff. The split child's request carries that answer, and the planner builds those staffs with it and doesn't ask the question again:

```python
# the split child's request
request.lane_layouts = {"{part_id}|{staff}": "shared" | "separate"}      # only the open staffs of this part

# the planner (src/api/preprocess_chooser.py, choose and choose_repair)
pins  = the lane_layout slot of each staff named in lane_layouts          # build_sheet(pinned=…) already supports this
sheet = build_sheet(score, planning_context, pinned=pins)                 # the slot is fixed, so it is not asked
with no lane_layouts (every split started in chat): nothing changes
```

A `lane_layouts` entry for a staff that isn't open, or a value other than shared or separate, fails the split with a planning error rather than being ignored.

**The call's roles and sounds, saved at "Sing it".** The reading stage writes them into the score summary with `apply_llm_program_assignments`, the function and checks the chat's `synthesize` uses, and saves the summary as that tool does (`set_score_summary`). So no code that runs later asks an LLM the same question: the chat model's quote sees no instrument waiting, and the instrumental files read the saved sounds as today.

```python
entries = []
for each instrument waiting for a ruling or a sound:              # the score summary's unresolved list
    declared instrument                                 → playback_preset = judgment.sounds[id], or none chosen when null
    undecided part with a ticked row                    → role not_instrumental            # it is sung
    undecided part answered voice                       → role not_instrumental
    undecided part answered instrument, no ticked row   → playback_preset = judgment.sounds[id], or none chosen when null
    undecided part answered unsure, no ticked row       → none chosen                      # a MIDI track for the user to set
summary, error = apply_llm_program_assignments(summary, entries, allow_user_choice=True)   # every instrument has an entry
if error: end("failed", "quote_changed")                 # the quote checked the same rules, so the score changed
set_score_summary(summary)
```

| The part | What "Sing it" saves | What the rest of the code sees afterwards |
|---|---|---|
| Undecided, with a ticked row, sung without a split | Not an instrument | A voice part. Its solfège line also gives it words, so code treats it as one at every later parse |
| Undecided, with a ticked row, sung after a split | Not an instrument, on the part the lines were made from | A voice part, and the lines made from it are voice parts. The chat model isn't asked about it. After the split, change 7 settles it the same way |
| Undecided, answered voice, not ticked | Not an instrument | A voice part that isn't sung yet; no MIDI track |
| Undecided, answered instrument, not ticked | The call's sound, or none chosen | Played in the instrumentals; with none chosen, a muted track whose dropdown says "Choose an instrument". The chat model isn't asked |
| A declared instrument the score gives no usable sound | The same | The same |
| Undecided, answered unsure, not ticked | None chosen | A muted MIDI track whose dropdown says "Choose an instrument" |

`src/musicxml/instrument_programs.py` changes only for changes 7 and 8. A sung part is saved as not an instrument, so it is never also played; every part not sung is a MIDI track.

**The children.**

| | Split child | Take child |
|---|---|---|
| Created by | The reconciler, through `_start_preprocess_job` | The reconciler, through `_start_synthesis_job` |
| Request | The same `start_preprocess_workflow` request the chat model writes, built in code: the part id, the verse and the quote's `lane_layouts`; no free-text instruction | Part id, voicebank, language, the row's lyric line, repeats, and the row's style controls from the quote (or the voicebank's defaults and default voice colour when there is no note); `require_solfege_lyrics` for a solfège take |
| Runs on | The preprocess worker, as today, on the planner's own model | The render job, or today's synthesis route (difference 11) |
| Marked | `parentJobId`, `parentItemId` | The same, plus the reservation's `parentJobId` |
| Song's current job | No. The chat stays free | No |
| Writes to the chat | Nothing. The card shows it | Nothing. The card shows it |
| Credits | None in step 3 | Its own reservation: settled when the take is made, released when it fails or stops, by today's functions |
| When it ends | Enqueues `reconcile(parentJobId)` | Adds its track to the player, then enqueues `reconcile(parentJobId)` |

A worker refuses a child whose parent doesn't list it, or whose parent is not `running`.

**The lyric line of a take.** Lyrics mode: the part's line for the chosen verse; a part with no line of that number uses its lowest-numbered word line. Solfège mode: the generated solfège line the solfège stage returned for that part. Both come from the summary's `lyric_selections`, through the helpers the chat path already uses.

**The player track.** The track key becomes (part, mode). `mode` is `solfege` when the take sang a generated solfège line, else `lyrics`. A track saved before this has no mode and counts as `lyrics`. This applies to chat takes too: a solfège take no longer replaces the lyrics take of the same part.

**While a Sing All is running** (HLD §7):

| Action | In the job's Song | In another Song |
|---|---|---|
| Chat message | Allowed | Allowed |
| A chat tool that changes the score or sings: split, add or change solfège, reparse, an edit, a quote, a take | Refused. The tool result says a Sing All is running, and the chat model tells the user | Allowed |
| Solfège settings | 409 | Allowed |
| Export mix, listen, download | Allowed | Allowed |
| Stop, Replace | Allowed; they stop the Sing All (S4) | As today |
| Delete the Song | 409 `work_running` | As today |
| "Sing it" | 409 `sing_all_already_running`, with the running job's Song | The same |

### Change

#### 1. Store, `src/backend/sing_all_store.py`

- `create_job_with_reservations` (the one transaction), `load`, `apply` (the reconciler's transaction), `clear_active`.

#### 2. Credits, `src/backend/credits.py`

- Built by Claude (5c5a37d on `feature/sing-all-core`); call it as it is:

  ```python
  ReservationItem(job_id, estimated_credits, billing_components=("vocal",), job_kind=None, pricing=None,
                  pricing_unit_seconds=None, billable_duration_seconds=None, instrumental_charge_scope_value=None)
  class ReservationGuard(Protocol):
      def read(self, transaction) -> Optional[str]   # reads only; a refusal reason, or None
      def write(self, transaction) -> None           # writes the job and users.activeSingAllJobId
  reserve_credits_for_items(uid, items, *, guard, session_id=None, score_id=None, score_version_no=None,
                            parent_job_id=None, reservation_ttl_seconds=None) -> ReserveCreditsResult
      # status "reserved" | "refused" (result.reason = the guard's) | "insufficient_balance" | "overdrafted"
      #        | "expired" | "infra_error"; all or nothing; every read happens before any write
  settle_instrumental_reservation(uid, reservation_id) -> SettleCreditsResult
      # "settled" | "already_settled" | "already_released" | "reservation_missing" | "reconciliation_required" | "infra_error"
  ```

  The instrumentals' item has `billing_components=("instrumental",)` and its charge scope (`instrumental_charge_scope(uid, session_id, score_id)`); its job id is `{jobId}-instrumentals`. A take's reservation is settled by today's `settle_credits_and_complete_job` and released by `release_credits`, both keyed by the child's job id.
- `settle_instrumental_reservation(uid, reservation_id)`, new: in one transaction, a pending instrumentals-only reservation is charged its reserved credits, and the score's instrumental charge record goes from pending to paid, as `settle_credits_and_complete_job` does for a take that carries them. A reservation already settled or released is a no-op. It has no job to complete, like `settle_export_mix_credits_and_complete_job` has its own path. `release_credits` already returns a pending instrumental charge to unpaid, so release needs no change.
- A reservation made here has no take quote bound to it, so releasing it expires nothing else. It carries `parentJobId`.

#### 3. Reconciler, `src/backend/sing_all.py` and `src/backend/orchestrator.py`

- `decide(job, children, score_state) -> (new job fields, actions)` is pure and lives in `sing_all.py`.
- `SingAllRunner` in a new file `src/backend/sing_all_runner.py` carries out the actions with the orchestrator's existing functions: start a split, add solfège, prepare the instrumentals, start a take, release a reservation, append history.
- `_start_preprocess_job` and `_start_synthesis_job` take an optional `parent`: the child is created under the given id only if absent, is not made the Song's current job, and writes nothing to the chat.
- `_run_preprocess_job` and `_run_synthesis_job`: in their `finally`, enqueue the parent's reconcile when the job has a `parentJobId`.

#### 4. The planner's entry point, `src/api/preprocess_chooser.py`, `src/backend/orchestrator.py`

- `choose` and `choose_repair` take the request's `lane_layouts` and pin those slots, as in the rule above. `choose_repair` already carries layout pins of its own; these join them.
- The call sites in `_plan_preprocess_v2` and `_plan_preprocess_v2_repair` pass `planning_context["lane_layouts"]` through.
- No prompt changes, and no change to what the planner asks when `lane_layouts` is absent.

#### 5. Reconcile queue, `src/backend/job_dispatch.py`

- A third route kind, `sing_all_reconcile`, with endpoint `/internal/sing-all/reconcile`. Its tasks are unnamed, so a second request for the same job is not swallowed by the first; `reconcile` is safe to repeat.
- Settings, like the other two routes: `SING_ALL_RECONCILE_DISPATCH_MODE` (`inline`, `http`, `cloud_tasks`), `SING_ALL_RECONCILE_URL`, `SING_ALL_RECONCILE_TASKS_QUEUE`, and the worker token and invoker account. `inline` runs it in the API process, for tests.

#### 6. Endpoints and guards, `src/backend/main.py`, `src/backend/orchestrator.py`

- `POST /sing-all/jobs`, `POST /internal/sing-all/reconcile` (worker callers only).
- The guard table above: in `_execute_tool_calls` for the chat tools, returning an `action_required` payload for the chat model; in the solfège settings and delete endpoints, a 409.

#### 7. Player, `src/backend/songs.py`, `src/backend/orchestrator.py`

- `mode` on the take's track data and on `vocals[]`; the key includes it.

#### 8. Messages, `src/backend/messages/en.properties`

- `sing_all.started_history`, `sing_all.ended_history`: one factual line each, with the counts and credits, so the chat model knows what happened.

### Scope

May change: the files above, `src/backend/session.py` and `src/backend/job_store.py` for the child fields, tests. `src/api/preprocess_chooser.py` only to accept `lane_layouts`. Must not change: every prompt, and the planner's rules and questions for a split started in chat; Stripe and billing endpoints; the take quote and consent path for chat takes; deploy scripts and prod env.

### Done when

- `decide` has one test per row of the tables above and per `case`, with no Firestore.
- Planner tests, on a made-up score with an open staff:
  - `lane_layouts` = separate: the sheet has no lane-layout question, and the split makes the separate layout's number of parts.
  - `lane_layouts` = shared: the same, for the shared layout.
  - No `lane_layouts`: the question is asked as today. The existing planner tests pass unchanged.
  - A `lane_layouts` entry for a staff that isn't open: a planning error.
- With the emulator, on made-up scores and stubbed workers:
  - A 4-part score with no split: 4 reservations at "Sing it"; 4 takes; 4 tracks in the player; status `completed`; the user's reserved credits back to 0 and the balance down by the total.
  - A score with an open staff quoted as separate: the split child's request carries that answer, the parts made match the rows, and the job completes with the quoted total charged.
  - A split that returns a different number of parts than quoted: status `failed`, reason `quote_changed`, every take reservation released, the guard cleared.
  - A split that fails: status `failed`, every take reservation released, the guard cleared.
  - Mode both: two takes and two tracks per part, keyed by mode.
  - One take fails: `partial`; that take is refunded and the others are charged.
  - With instrumentals: the first take fails and the rest are made: the instrumentals are charged once, when the second take is made, and the score is marked paid. Every take fails: the instrumentals are refunded and the score stays unpaid. `reconcile` run twice after a take is made: charged once.
  - A second "Sing it" while one runs: 409, and no reservation is written.
  - Not enough credits: 409, no job, no reservation.
  - "Sing it" after the score changed: 409 `quote_changed`, no job, no LLM call.
  - A whole job from "Sing it" to `completed` with the LLM stub asserting it is never called.
  - Change 7 in chat: a part with no words and no declared instrument is split; the next chat quote lists nothing waiting for it, a sound sent for it is rejected, and the instrumental files leave it out. The same after a reparse. A declared instrument that is split, and a part not split, are unchanged.
  - A part with no words and no declared instrument, quoted as a voice: it is sung in solfège, and the summary has it as not an instrument. Quoted as an instrument and not ticked: the summary has the call's sound and the instrumental files play it. A declared instrument with no program: the same. Afterwards the summary lists no instrument waiting, so the chat's quote asks nothing.
  - A part quoted as unsure and not ticked, and an instrument whose sound is null: saved with none chosen; the instrumental files have a track for it with no program, marked for the user to choose, melodic or drum as the part is.
  - `reconcile` run twice in a row, and two at once: no child is started twice and no credit moves twice.
  - Each row of the "while a Sing All is running" table.
- `tests/test_credits.py` and `tests/test_credit_retry.py` pass unchanged.

### Out of scope

`needs_decision`, re-quote and "Retry failed parts" (step 6). Mix items and the zip (step 4). Emails (step 5). Charging splits (step 8). Anything the chat model does for Sing All beyond S1 change 9: it opens the card, and the card does the rest.

---

## S3. Progress and UI

### Rule

`GET /sing-all/active`:

```python
job = singAllJobs[users.activeSingAllJobId] if set and not ended
return {"job": progress(job) or None}
```

`progress(job)` is the API contract's `Progress`, returned by `GET /sing-all/active`, `GET /sing-all/jobs/{id}` and "Sing it".

`remainingS` is rough: about 4 minutes for each split still to run, plus 1 minute to start singing and the sung length once. The constants are settings.

**The page.**

| Where | What it shows |
|---|---|
| Starters under the chat input | One "Sing All" button, when the Song has a score. The three prototype buttons are removed |
| Tapping it | Sends the visible chat message "Sing All". The chat model checks the language (S1 change 9); its reply opens a card in that bubble, filled from `POST /sing-all/quote` with the language it gave. It is not saved until "Sing it" |
| Card, while the quote is made | "Reading the score…" in place of the voices and the price. The first quote for a score waits for the quote call; later ones don't |
| Card, the quote couldn't be made | "The quote couldn't be made." with "Try again", which asks for the quote again. No "Sing it" |
| Card, before "Sing it" | The summary line, the voices, the style lines when a note is applied, the quote lines, "▸ Customise" and "Sing it · N cr": the "Sing All (3)" layout, with real data. "Sing it" is shown only here, never while Customise is open |
| Customise | Language (the dropdown above), Parts and voices (tick, voice), Sing in (Lyrics, Solfège, Both), Verse, Repeats. Every change asks for a new quote, which reuses the saved answer and makes no new call, and doesn't send the note. Style notes: a 2-line text box. In place of "Sing it", the button is "Apply": it sends the note with the choices, and always folds back to the summary, which shows the style's values. "Reset" returns to the defaults and clears the note. Mixes are not shown in step 3 |
| "Sing it" answers `quote_changed` | The card asks for a new quote with the same choices and shows it, with one line: "The score changed, so here is the new quote." Nothing has started |
| Card, running | The stage list with counts ("Splitting the vocal parts (1 of 2)", "Singing (3 of 8)"), each row's ticks, elapsed time, the rough time left, "You can close this page. Your song will be here when you come back.", and Stop |
| Card, ended | Completed, partial, failed or cancelled; which parts were sung; the receipt |
| Header pill | "Sing All · Singing 3/8", from any Song, linking to the job's Song |
| Player | A track appears as each take finishes. With takes in both modes, a Lyrics / Solfège switch above the tracks shows one mode's takes at a time |
| After a reload | The saved history entry with `singAllJobId` shows the card again, from `GET /sing-all/jobs/{id}` |

The page asks `GET /sing-all/active` once on load and after sign-in. While a job is running it polls at the interval today's job progress uses, from any Song. When a take turns `done` in the Song on screen, the page reloads that Song's player from `GET /songs/{id}/workspace`.

In the job's Song the chat input stays usable. Send works; the starter buttons for singing and splitting are disabled with "A Sing All is running".

### Change

#### 1. Endpoints, `src/backend/main.py`, `src/backend/sing_all.py`

- `GET /sing-all/active`, `GET /sing-all/jobs/{id}` (owner only, else 404), `progress(job)`.
- `GET /songs/{id}/workspace` returns each history entry's `singAllJobId` and each vocal's `mode`, and leaves Sing All children out of `jobs`.
- The API contract's other S3 rows: the quote's added fields, and "Sing it" answering `Progress`, with `activeSongId` on `sing_all_already_running`.

#### 2. UI, `ui/src/components/SingAllCard.tsx`, `ui/src/MainApp.tsx`, `ui/src/api.ts`

- `SingAllCard` replaces `SingAllPrototypeCard`: the quick-inline layout and its CSS are kept, the fake data and the other two variants are removed.
- `MainApp.tsx`: the one starter button, the message field `singAllJobId`, the header pill, the poll, the player's mode switch.
- The player's instrument track with nothing chosen (S1 change 8): today it assumes a drum kit (bank 128, "Choose a kit"). It takes the part's own percussion flag instead, so a melodic one shows "Choose an instrument" and lists melodic sounds. It stays muted until the user picks, as today.

### Scope

May change: the files above, `ui/e2e`, tests. Must not change: the Songs pages' behaviour, the take card for chat takes.

### Done when

- Backend tests: `GET /sing-all/active` with no job and with a job at each stage; `GET /sing-all/jobs/{id}` for the owner and for another user.
- Backend tests for the API contract's S3 rows: every `Progress` field name and value set, on a job at each stage and each status; the quote's `voices`, `options` and structured `notes` on made-up scores (one whose repeats can't be followed, one with an instrument whose sound is null); "Sing it" answering `Progress`; a second "Sing it" carrying `activeJobId` and `activeSongId`; a Sing All child left out of the workspace's `jobs`.
- Playwright, mocked API: the card with the defaults; "Reading the score…" while the quote is pending; a failed quote with "Try again" working on the second attempt; `quote_changed` at "Sing it" showing the new quote; a change under Customise asks for a new quote and updates the total; "Upgrade to sing" on a free plan; "Top up" without enough credits; the running card through each stage; the ended card for each status; the pill from another Song; a reload showing the card again; the mode switch.
- Playwright on the real local backend (`ui/e2e/run.mjs`, regression LLM): a made-up 2-part score that needs no split goes from the button to two tracks in the player, and the receipt matches the credits taken. The harness has one voice installed and the score has no staff to decide and no undecided part, so this run makes no quote call and the regression LLM is unchanged.
- Checked at phone width.

### Out of scope

The zip, the mix line, Share and "Retry failed parts" on the card. Swapping the score preview's syllables (difference 12).

---

## S4. Stop for the parent, and the sweeper

### Rule

```python
# POST /sessions/{id}/stop and Replace, when this Song has the running Sing All
job.cancelRequested = True
for child in the job's running children: job_store.stop_job(child)     # each answers stopped or too_late
reconcile(job)

def wind_down(job):                    # the reconciler, once cancelRequested is set
    start nothing new
    items not started: status stopped, release their reservations
    if a child is still running: return                  # it is past its stop point and will finish
    if every take item is done: status = "completed"     # Stop was too late for all of it
    else:                       status = "cancelled"
    stage = "done"; clear the guard; add the history entry
```

| When Stop arrives | The job | Credits |
|---|---|---|
| Reading, splitting or solfège | `cancelled`. A split before its stop point is dropped (task B); one past it is saved and stays in the Song | Everything refunded |
| Singing, some takes finished | `cancelled`. Finished takes stay in the player and on the card | Finished takes, and takes past their stop point, are charged; the rest are refunded |
| Singing, every remaining take past its stop point | `completed`, as if Stop was never pressed. The answer is "Too late to stop: it's just finishing" | All charged |
| The job has already ended | Nothing changes | — |

The Stop button asks first: "Stop Sing All? Parts already sung are kept and charged. The rest is refunded." Replace on the job's Song asks the same thing in its own popup, then replaces the score.

The stop response gains `singAll`, and never lists a Sing All child in `jobs`: the API contract's `POST /sessions/{id}/stop`.

**The sweeper.** `POST /internal/sing-all/sweep`:

```python
for job in singAllJobs where status == "running":
    reconcile(job)                                         # picks up a lost notification
    limit = 30 min for the running split child             # the clock restarts for each child
            20 min with no new take, counted from when singing really started (not time spent in line)
    if no progress within the limit:
        fail the stuck children, release their reservations, reconcile(job)
```

### Change

- `src/backend/main.py`: `_stop_song_work` handles the Song's running Sing All; the sweep endpoint, for worker callers only.
- `src/backend/sing_all.py`: `wind_down` and the stuck test inside `decide`.
- `ui/src/MainApp.tsx`, `SingAllCard.tsx`: the confirm step; Stop on the card and next to Send.

### Done when

- One test per row of the Stop table, with the user's credits checked after each.
- A test with a lost child notification: the sweep completes the job.
- A test with a split child with no progress for 31 minutes and one with no new take for 21 minutes: the stuck items fail and are refunded, the job ends, the guard is cleared. Time is injected, not slept.
- Replace during a Sing All: the job is cancelled and the new score is in place.
- The stop response's `singAll` for `stopping` and for `too_late`, and a Sing All child never listed in its `jobs`.

### Out of scope

Stopping inside one take's stage (HLD §18). The email after a sweep.

---

## S5. The render job and its line

### Rule

```python
# the reconciler, entering "singing" with SING_ALL_SINGING_TRANSPORT = "render_job"
in a transaction on singAllRender/slots:
    if len(running) < MAX_RENDER_SLOTS:  running += job; start one execution of the render job for this job
    else:                                job.render.state = "waiting"; job.render.queuedAt = now
# when a job's takes have all ended, or its execution ended
    free its slot; start the oldest waiting job

# the render job:  python -m src.backend.sing_all_render   (env SING_ALL_JOB_ID)
for each take child of the job that hasn't ended, up to SING_ALL_RENDER_CONCURRENCY at a time:
    if the parent has cancelRequested: stop
    claim the child and run it with the code the synthesis worker runs       # settle, player track, reconcile as today
enqueue reconcile(job)
```

| Setting | Default | Production |
|---|---|---|
| `SING_ALL_SINGING_TRANSPORT` | `dispatch` (today's route, one task per take) | `render_job` |
| `MAX_RENDER_SLOTS` | 1 | 1 |
| `SING_ALL_RENDER_CONCURRENCY` | 4 | 4 |
| `SING_ALL_RENDER_JOB` | empty | The Cloud Run Job's full name |

A waiting job shows "Queued for singing · 2nd in line": `Progress.render.placeInLine` is its place among the waiting jobs, oldest first, 1 = next to start, and `null` when it isn't waiting. There is no fallback to the L4 (HLD §17.5). An execution that ends with takes not made is started once more; it skips takes that already exist. After that the remaining takes fail and are refunded.

### Change

- New file `src/backend/sing_all_render.py`: the entry point and the slot transaction.
- `src/backend/sing_all_runner.py`: start an execution through the Cloud Run Jobs API.
- New scripts `scripts/build_sing_all_render_prod.sh` and `scripts/deploy_sing_all_render_prod.sh`, modelled on the Blackwell worker's. Written for Alan to read and run; not run from this session.

### Done when

- Tests with a stubbed Jobs API: one slot, two jobs: the second waits and shows its place, and starts when the first ends.
- A test of the entry point on stubbed synthesis: 6 takes, at most 4 at once; a second run makes only the missing takes.
- A test that Stop between takes ends the run.

### Out of scope

Batching several jobs into one execution (HLD §17.5, later).

---

## S6. Deployment (Alan)

Not done from this session. In order:

1. Planner v2 is live for everyone (HLD §5.1). Sing All's splits run on it.
2. Create the `sing-all-reconcile` Cloud Tasks queue in us-east4, up to 50 concurrent dispatches.
3. Build and deploy the `sing-all-render` Cloud Run Job: RTX PRO 6000, us-central1, `MCP_GPU_WORKERS=4`, task timeout 30 minutes, retries 1.
4. `prod.api-cpu.env`: `SYNTHESIS_PREMIUM_ROUTING=off`, `SING_ALL_SINGING_TRANSPORT=render_job`, `SING_ALL_RENDER_JOB`, and the reconcile queue settings. The quote call needs no new line: without `GEMINI_SING_ALL_QUOTE_MODEL` it runs on the chat model. Set that line, and `GEMINI_SING_ALL_QUOTE_THINKING_LEVEL` and `GEMINI_SING_ALL_QUOTE_TIMEOUT_SECONDS` if needed, to move it to another model.
5. Cloud Scheduler: `POST /internal/sing-all/sweep` every 5 minutes.
6. The CPU API's service account may run the render job and add tasks to the new queue.
7. Deploy the CPU API, then the UI. No Firestore index is needed: the sweep reads `singAllJobs` by `status` only.

---

## Checks (every task)

```bash
P=/Users/alanchan/antigravity/ai-singer-diffsinger/.venv310/bin/python
FIRESTORE_EMULATOR_AUTOSTART=0 $P -m pytest tests/test_backend_api.py tests/test_job_store_progress.py tests/test_job_dispatch.py tests/test_credits.py tests/test_credit_retry.py tests/test_mcp_progress_firestore.py tests/test_mcp_server.py tests/test_mcp_end_to_end.py tests/test_premium_synthesis_worker.py tests/test_synthesis_pricing.py tests/test_audio_mix.py tests/test_songs_backend.py tests/test_preprocess_chooser.py tests/test_preprocess_sheet.py tests/test_preprocess_planner_v2_wiring.py tests/test_llm_gemini.py tests/test_llm_openai.py -q -p no:cacheprovider
```

Known baseline: 12 failures in `tests/test_backend_api.py`. The planner and LLM test files are added to the command for S1 and S2; their baseline is taken before the first change. No test calls a real LLM. New test files: `tests/test_stop_split.py`, `tests/test_sing_all_estimate.py`, `tests/test_sing_all_judgment.py`, `tests/test_sing_all_quote.py`, `tests/test_sing_all_reconcile.py`, `tests/test_sing_all_backend.py`, `tests/test_sing_all_render.py`, and `ui/e2e/specs/sing-all.spec.ts`. The UI checks are `npm run build` and the Playwright specs, as in step 2.

---

## Notes for Alan

- **The branch.** `feature/sing-all-prototype` holds Songs (4 commits, not pushed). `origin/feature/sing-all` holds the lane slot change. Step 3 needs both, so the first act of coding is merging `origin/feature/sing-all` into this branch. That needs your word.
- **Planner v2.** The estimate is built on v2's skeleton, and a split child is given the quote's lane answer, which only v2 can take. A split run by the old planner ends the job as `split_failed`. Local tests stub the split; a real split end to end needs v2 on.
- **Planner code is touched** (S2, change 4). `choose` and `choose_repair` take the quote's lane answer and fix that question. The prompt, the rules and every split started in chat are unchanged, and the planner's own tests must pass unchanged. This is the Part Splitting thread's code, so it needs your OK.
- **Two new prompts that refer to the current ones** (S1; your call, 2026-11-15). Each new file holds only Sing All's task, reply format and differences, and names the sections to follow. The chat prompt's fixed text (about 42 KB) and, for the quote, the planner's prompt (about 6 KB) are sent after it as references. A change to a rule reaches Sing All with no second edit, and changes the input hash, so saved answers are asked again. The cost: each quote call sends about 48 KB of prompt, most of it not about Sing All, so the model must keep to the named sections. It is once per score.
- **The lane answer on the normal model is untested.** Tests use a stubbed LLM. No comparison with the planner's model now (your call, 2026-11-15): if quoted line counts are often wrong in testing, `GEMINI_SING_ALL_QUOTE_MODEL` moves the call to another model.
- **The quote now costs an LLM call**, once per score and again after each score change. It is not charged to the user; worth pricing before launch.
- **The call now has four questions** (2026-11-15): voice part or instrument, an instrument's sound, shared or separate, and the voice. The first is asked only for a part with no words and no declared instrument that the chat model hasn't ruled on. The sound is asked for each instrument the score gives no usable sound, and for each such part answered instrument.
- **No existing code asks again.** "Sing it" saves the roles and sounds into the score summary with the function the chat's `synthesize` uses, so the chat model's quote finds nothing waiting. The two places that ask the chat model (`prepare_synthesis_quote` and `synthesize`) are not on Sing All's path.
- **Chat changes too** (S1, change 7; your call, 2026-11-15). A part with no words and no declared instrument that is split in chat becomes a voice part in code, so the chat model isn't asked about it again and it is never played under its own sung lines. It ships with this spec, not as a separate fix on main.
- **The saved sound is only the starting sound.** The player's instrument dropdown changes a track's sound, and the Song's player record keeps that choice, as for any take today.
- **Only vocal rows can be left out** (your call, 2026-11-15). Every part not sung is a MIDI track. When no sound is chosen (the call answers null, its sound fails the check twice, or the part is unsure and unticked), the track is muted with "Choose an instrument" in the player. This needs S1 change 8: today only a drum part can be a track with nothing chosen. Chat keeps its rule that the chat model must choose a sound.
- **Step 4's server mixes** will need to leave out a track with no instrument chosen, as the player keeps it muted. Noted here so step 4's spec picks it up.
- **`credits.py` is touched** (S2, change 2): `reserve_credits`' body moves into a shared function, and one new function settles the instrumentals' own reservation (your call, 2026-11-15). No pricing rule changes, and the existing credit tests must pass unchanged. This is billing code, so it is built here, not by Codex. All reservations are written in one transaction (your call, 2026-11-15).
- **The instrumentals' price was wrong in an earlier draft.** It is today's rate, 1 credit per started 2 minutes of the sung length, not a flat 1 credit; the quote now uses it.
- **The split request has no user message.** In chat, the planner context carries the user's words. Sing All sends the part and the verse only. If v2 turns out to need a sentence there, I'll come back with the wording rather than invent one.
- **A part split selectively in chat** (the user asked for one line only) counts as already split, so Sing All sings only that line of it. The user can split the rest in chat first.
- **Solfège when no voice sings the song's language** (your call, 2026-11-15): always English with the solfège pronunciation fix, which respells "do" as "doh" for the English dictionary and acts only for English. An English voicebank is always enabled. The card then quotes solfège only.
- **The chat prompt changes** (S1 change 9; your call, 2026-11-15): one new section and one new tool, `open_sing_all`. A typed "Sing All in Greek" goes through the same check as the button, so chat entry comes in step 3 too. Its wording is a draft for you.
- **Two rows can get the same voice.** The call picks the best fit per row, by the chat model's rule. The mockup shows four different voices; a "prefer a voice not used yet" line in the prompt would be a new rule, so it isn't in.
- **A free-plan user who bought a top-up** sees "Upgrade to sing": the rule stays `activePlanKey == "free"` (your call, 2026-11-15). You plan to allow top-ups only for paid subscriptions, which is a separate change.
- **Chat takes get a mode too** (S2; your call, 2026-11-15). A solfège take in chat sits beside the lyrics take of the same part instead of replacing it, with the switch above the tracks; a new take of a part replaces only the track of its own mode. The player follows one rule whoever made the take.
- **Two history lines per job** are fixed English text from the message catalog, like the stop lines. They exist so the chat model knows a Sing All ran.
- **Not measured yet:** how long the first quote takes. The code part is quick; the wait is the call, and its time on the chat model is unknown until it runs with a real key. Each call logs its time. Later quotes for the same score make no call.
- **Still open from step 2**, unchanged: the harness teardown order; a live Replace showing "Stopped." without the refund line until reload; 2 mocked e2e specs that fail on the parent (demo-songs, score-history); the client rules task (C3).
