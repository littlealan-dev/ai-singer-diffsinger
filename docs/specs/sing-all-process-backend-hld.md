# Sing All: process and backend (HLD)

Status: design for Alan's review, updated 2026-11-03. No code yet.

Scope note: Songs (§2.1), the saved player state (§4) and the share link (§14) apply to every user, free and paid, not only to Sing All.

Builds on:
- `render-all-and-premium-gpu-worker-hld.md`: a parent job drives today's child jobs, through a reconciler.
- `sing-all-quick-mode-proposal.md`: the UI, quick mode plus Customise, in the chat bubble. Alan chose this as the final UI.

## 1. Decisions so far (Alan, 2026-11-01)

| Topic | Decision |
|---|---|
| UI | The in-chat quick mode with Customise ("Sing All (3)" prototype) is final |
| After "Sing it" | Tell the user they can wait or close the page; an email arrives when the job ends |
| Concurrency | One Sing All job per user at a time, across all their Songs |
| During a Sing All | In its Song: chat only, no edits or singing, until it ends or is cancelled. Stop and Replace are allowed (§7.1). **In other Songs: chat, edits, splits and single takes are all allowed** (Alan, 2026-11-03, replaces the earlier block on all singing). Only a second Sing All is refused |
| Progress | Survives refresh and logout/login; shown stage by stage (split, solfège, singing, mixing, packaging) |
| Split fails or is partial | The job **pauses** ("needs your decision"). The user reviews the partial split; the quote is updated to the parts that worked; they press Sing it again. Auto-cancel with refund after 24 h |
| Solfège fails | Lyrics also chosen: carry on with lyrics only, refund the solfège items, note it. Solfège only: pause, as above |
| Synthesis fails | Load every take that worked; refund the rest item by item; offer "Retry failed parts" |
| Stop | **App-wide** (Alan, 2026-11-03): a Stop button in the chat stops the running work, whichever kind: chat reply, split, solfège, take or Sing All. Replacing the score does the same stop, then uploads. **If it's too late, the stop or replace is discarded and the job completes as if it was never clicked** (Alan, 2026-11-03). Details in §7.1 |
| Credits | Reserve the whole quote at "Sing it", including lanes that may not be needed; settle and refund item by item; show a receipt |
| Splitting is charged | **Starts at public launch** (Alan, 2026-11-03). Planner v2 is live first, so the rate is set from its real prod cost. Until launch, splits stay free everywhere, and the Sing All quote shows the split line as **included** (0 credits) for allowlisted testers. Once enabled, **every** split, including chat splits, is charged per successful output part: **1 credit per started 30 s** of written-order length. Length is the written order (no repeats); each part is rounded up like singing; the planner's own repair attempts are free; a split the user asks for again is charged again; failed or review-required parts are refunded. A chat split gets a quote and confirmation step like singing |
| Emails | English only. Sent on completed, partial, needs-decision and failed. The job link inside is owner-only |
| Job link (email) | Owner only; anyone else sees "This job isn't yours" and "Ask the owner for a share link" |
| Opening a job | Opens the job's Song with the job summary as its latest message, plus the score, all tracks and the saved mix. It shows the Song as it is now, not as it was at the job's end |
| Lyrics / solfège toggle | Above the score preview when the job has both; it swaps the score's syllables and the player's takes |
| Player | Loads the solo takes, mixed live as today; the rendered mixes are in the zip only |
| Zip | Built when the job finishes; the one frozen result. Solo vocals are real WAVs for now, for troubleshooting (Alan, 2026-11-03) |
| My songs | Lists every Song, free and paid: open, share, delete |
| Share | Read-only permalink, no sign-in needed. A signed-in viewer still sees it read-only, never as their own job. **Live link for any Song, free or paid** (Alan, 2026-11-03): each page load shows the Song's latest score, tracks and saved mix, with no frozen versions and no live push; viewers refresh to see changes. Plays in the browser with the same engine as the owner's player. Mute/unmute per part; the owner can turn it off; listen only, no download |
| Song | One Song has one chat and one current score (Alan, 2026-11-03). In code it's today's session. It's created at the first score upload or demo song; "New song" opens the upload screen. Uploading into a Song that has a score asks "New song or replace?" (§2.1) |
| Saved player state | Every Song saves its tracks, mute, solo, volume and instrument choices on the server (Alan, 2026-11-03). Sing All writes to the same record (§4) |
| Server mixes | Mix MIDI and vocals on the backend with automatic balance; in a part-dominant mix the vocal is significantly louder than the instruments (Alan, 2026-11-03, §9.1) |
| Unifying the backend | Stage 1 and stage 2 approved (Alan, 2026-11-03, §16). Moving chat takes onto the job model (stage 3) is not approved yet |
| Retention | Paid plans: kept indefinitely. Free: a share link stops 30 days after the owner's last change (Alan, 2026-11-03) |
| Free users | The quote shows "Upgrade to sing". A later free 30-second preview must fit without rework (§12) |
| Quote-time split estimate | When the quote opens, a quick check with no LLM finds which parts will be split and the most lanes each staff could need. The quote lists and reserves that worst case, and lanes not used are refunded (Alan, 2026-11-05, §5.1) |

## 2. Terms

- **Song:** what the user sees as one piece of work: one chat, one current score with its versions, its tracks and mix, and at most one share link. In code it's a session.
- **Sing All job** (the parent): one user's run, from "Sing it" to the end. It has a quote, items, a stage and a status.
- **Item:** one chargeable piece of work.
  - **split**: one output part.
  - **take**: one part in one singing mode.
  - **instrumental**: once per job.
  - **mix**: one file, either a full mix or a part-dominant mix.
- **Child job:** today's existing job records (`jobKind` preprocess, synthesis, export_mix). The parent creates them and reconciles their results.
- **Player record:** a Song's saved state: its tracks, each track's mix, the instruments and the score version in use. Chat takes and Sing All write to it; the owner's player and the share page read it.
- **Mix:** the mute, solo and volume settings, instrument choices and instrumental bus level in the player record.

### 2.1 The Song flow

```text
/app (no Song open)
  Shows: upload box, demo songs, My songs
  Chatting first works as today; nothing is listed yet

Upload a score or pick a demo
  → Song is created, titled from the score (can be renamed)
  → URL becomes /app/song/{id}
  → Any chat so far stays with it

Inside the Song
  Chat, splits, edits → same chat, new score versions
  Take finishes       → added to the Song's tracks
  Mixer or instrument → saved automatically
  Sing All            → a job in this Song, with its card in this chat
  Share               → one live link per Song

Coming back
  Reload              → same URL, same Song
  My songs            → any Song
  Job email link      → the Song, showing that job's card
  Owner on share page → "Open in SightSinger"

New song button → /app → upload → new Song with an empty chat
```

**Uploading into a Song that already has a score:**

```python
on upload or demo pick, when the Song has a score:
    ask "New song or replace?"                        # New song is the default
    if work is running in this Song: the dialog says what Replace will stop and what it costs (§7.1)
    case "New song":                                  # most re-uploads in the reports are a different song
        create a session, upload there, open /app/song/{new}
        this Song is unchanged, and its work keeps running
    case "Replace":
        parse and validate the new file first         # a bad file stops nothing
        result = stop_song_work(session)              # the same function as the Stop button (§7.1)
        if result == too_late:                        # the Replace is discarded
            the job completes as normal; when it has, ask "New song or replace?" again
            return
        new scoreId, version 1, preprocess history and solfège settings reset (as today)
        chat stays; old takes in chat bubbles still play
        player record reset to the new score: tracks and mix cleared (as today)
        title follows the new score unless the user renamed the Song
        same Song, same share link; if shared, the dialog says viewers will see the new score
```

The upload API has no check for running work today. It will refuse an upload over running work unless the request says the user chose Replace, so a stale page can't overwrite anything.

The reports show 38 of 313 sessions with score IDs held more than one score, and 31 of those 38 had different file names.

**Switching Songs:**
- A running job keeps going: synthesis and preprocess run as jobs on the server, and a client leaving only stops its progress stream. Finished takes reach the player record on the server.
- **"New song" and My songs are disabled while a chat reply is in progress.** That turn runs inside the `POST /chat` request. Cloud Run throttles CPU once the client leaves, and the HTTP middleware may also cancel it, so a reply could be lost. It's short, so waiting costs little. Closing the tab can still lose a reply, as it can today: the user's message stays and they can send it again.
- The client ignores late responses from the Song it left, using the generation guard re-upload already has (`workspaceGenerationRef`).
- Opening a Song resumes polling for its active jobs.

## 3. User flow and states

```text
                    ┌──────────── cancel ─────────────┐
"Sing it" → queued → running(stage) ──→ completed      ▼
                         │         ├──→ partial      cancelled
                         │         └──→ failed
                         └──→ needs_decision ──(user continues: new quote)──→ new job
                                    └──(24 h, no answer)──→ cancelled
```

| Status | Meaning | Credits | UI card | Email |
|---|---|---|---|---|
| queued / running | Working; `stage` says where | Reserved | Live stage list + "You can close this page" | — |
| completed | Every item made | All settled | Open results · Download zip · Share · receipt | "Your song is ready" |
| partial | Some takes made, some failed | Made: settled; failed: refunded | What failed + Retry failed parts · results · receipt | "Your song is ready (some parts failed)" |
| needs_decision | Split (or solfège-only) failed; waiting for the user | Split items settled or refunded per part; the rest still reserved until the decision, then released | Review the split · Continue with N parts · Cancel | "Action needed" |
| failed | Nothing usable | All refunded | What happened + what to try | "We couldn't sing your song" |
| cancelled | User cancelled, or the 24 h decision window passed | Not-started items refunded | Cancelled + receipt | — (only for the auto-cancel) |

**Stages shown**, each with a per-part breakdown and elapsed time:
1. Reading the score
2. Splitting the vocal parts (2 of 3)
3. Adding solfège
4. Singing (5 of 8: Soprano ✓ ✓, Alto ✓ …)
5. Mixing
6. Preparing the download
7. Done

The same status shows as a small pill in the app header, e.g. "Sing All · Singing 5/8", so it stays visible after the chat bubble scrolls away.

## 4. Data model (Firestore)

The job is the **process**: what was asked for, its progress and its credits. The player record is the **result**: what exists now and how it should sound. Chat and Sing All differ only in how they fill the record, so everything that reads it works for both.

```text
sessions/{sessionId}                      # a Song in the UI
  existing: userId, history, scoreId, version chain
  new:      title, songCreatedAt, shareToken?, deletedAt?
            # My songs lists only documents with songCreatedAt

sessions/{sessionId}/player/current       # NEW: one per Song
  ownerUid, scoreId, revision, updatedAt
  scoreVersions: {lyrics?, solfege?}      # the latest version per mode; a chat session usually has one
  measureMaps: {written, expanded}, repeats: bool, defaultMode: lyrics|solfege
  instrumentalMidi: {original?, expanded?}   # storage paths
  vocals: [{key, partId, label, mode, verse, sourceJobId, takeSignature, expandRepeats,
            durationS, voiceName, voiceCredit, muted, solo, volume}]
  instruments: [{key, rawPartId, label, percussion, unassigned, muted, solo, volume,
                 gmProgram, soundfontBank, presetKind}]
  bus: {muted, solo, levelDb}

singAllJobs/{jobId}                       # the process and the receipt
  uid, sessionId, scoreId, title
  kind: "full"                      # later: "preview" (§12)
  status: queued|running|needs_decision|completed|partial|failed|cancelled
  stage: reading|splitting|solfege|singing|mixing|packaging|done
  choices: {parts:[{partId, voice}], mode: lyrics|solfege|both, verse, repeats, mixes, styleNote}
  quote: {quoteId, items: [...], totalReserved, presentedTurnId}      # same consent rule as today
  items: [ {itemId, type: split|take|instrumental|mix, partId?, mode?, range: {startS, endS}|null,
            unitSeconds, estimatedSeconds, creditsReserved, creditsCharged, status, childJobId?, error?} ]
  finalScoreVersion, zipPath, summary
  decisionDeadline?               # needs_decision + 24 h
  createdAt, updatedAt, lastProgressAt, endedAt, emailSentFor: [status]

users/{uid}
  activeSingAllJobId?             # the one-at-a-time guard, set and cleared in transactions

shares/{shareToken}               # public lookup; the page never sees the owner
  sessionId, ownerUid, enabled, createdAt
```

The player record is a separate document from the session because the session document is already busy and large (chat history), and dragging a fader would add frequent writes to it. Sessions from before launch have no title or player record, so they aren't listed.

**Keeping it up to date:**

```python
on take completed (chat or Sing All child):      # added after the take is settled; a failure here is logged and never fails the take
    p = player(session)
    if take.scoreId != p.scoreId: return         # the score was replaced while this take ran
    if take.expandRepeats != p.repeats: drop the takes of the other setting; p.repeats = take.expandRepeats
    replace the track with the same (part, mode), keeping its mute/solo/volume; else append it

on score version change: p.scoreVersions[mode] = version; refresh measureMaps and instrumentalMidi

on score replaced (§2.1): reset p to the new scoreId with empty tracks and default mix

on owner's mixer or instrument change:           # debounced about 1 s; last write wins
    PUT /sessions/{id}/player/mix

def usable_tracks(p):                            # same rule as the player today
    return [t for t in p.vocals if not t.takeSignature or not current_signature(t.partId)
            or t.takeSignature == current_signature(t.partId)]
```

`range` and `kind` exist now, so a future free preview needs no data migration: a preview is a job whose takes have `range = {0, 30}`.

## 5. The pipeline (the parent reconciler)

### 5.1 Before the quote: estimating the split parts

The quote has to list and price parts that don't exist yet, because they come out of the split. So `POST /sing-all/quote` first runs a quick estimate from the parsed score:
- no LLM and no cost;
- well under a second;
- cached per score version.

```python
def estimate_sing_all_parts(score):
    parts = []
    for part in score.parts:
        if not is_vocal(part):                            # instruments -> the "Instrumentals" line
            continue
        for staff, counts in part_lane_counts(score, part_index=part.index).items():
            if counts.most == 1:
                parts.append(Estimated(part, staff, split=False))         # sung as it is
            else:
                for rank in range(counts.most):          # top line first
                    parts.append(Estimated(part, staff, split=True, rank=rank,
                                           name=lane_name(part, staff, rank)))  # "Women" -> Soprano, Alto;
                                                                                  # else "Women · voice 1"
    for p in parts:
        p.split_seconds = written_order_seconds(score)    # a split part lasts as long as its staff
        p.sing_seconds  = p.split_seconds if not repeats else expanded_seconds(score)
        p.voice = recommended_voice(range_of(p))          # today's recommended-voice logic, on that row's notes:
                                                          # the v2 skeleton with lane_layouts = separate where most > shared
    return parts
```

The quick-mode summary ("4 vocal parts"), the parts table (✂ marks the parts to be split), and the split and sing lines of the quote all come from this list.

**How many lanes** (Alan, 2026-11-05, from the Part Splitting improvement session). Voice tags don't always mean separate singers, and only reading the score can tell. So the quote reserves the worst case, and the LLM decides at split time. Alan prefers over-reserving and refunding to under-reserving and pausing.

```python
# src/api/voice_parts.py. One function, shared by the quote and the split's validator. No lyrics, no LLM.
sheet_lane_counts(score, *, part_index, staff_scope) -> LaneCounts(peak, real_voices, most)
part_lane_counts(score, *, part_index) -> {staff_scope: LaneCounts}    # the quote calls this per vocal part

# peak        = most notes sounding at once on the staff
# real_voices = distinct voice parts, excluding _default
# most        = max(peak, sum of each real voice's own peak)
shared = max(peak, real_voices)

quote:      reserve most lanes
validator:  peak <= lanes <= most
split (v2): if most == shared: lanes = shared     # no LLM question, so the quote is exact
            else: the LLM answers one question for the staff:
                      "shared"   -> shared lanes  # one group of singers written in two voices
                      "separate" -> most lanes    # each voice is its own group of singers
after the split: refund the lanes not used
```

| Staff | peak | real voices | most | shared | Quote |
|---|---|---|---|---|---|
| One voice with two-note chords | 2 | 1 | 2 | 2 | 2 parts, exact |
| Two voices taking turns, no chords | 1 | 2 | 2 | 2 | 2 parts, exact |
| Voice 1 with a two-note chord, plus voice 2 | 2 | 2 | 3 | 2 | "2 or 3 parts", the LLM decides |

**What the user sees** when a staff has `most > shared`:
- the staff says "2 or 3 parts", and the summary and total say "up to": "up to 3 parts", "up to N credits, unused parts are refunded";
- the parts table lists `most` rows for that staff, and their takes and part-dominant mixes are reserved like any other;
- the receipt shows the refund as "not needed".

**Not enough credits:** the balance is checked against the "up to" total, so a user can be told they lack credits for a job they could afford (worst seen: a staff reserving 9 lanes that probably needs about 4). They get the usual message: top up, or leave some parts out. Leaving a part out just means it isn't sung, whatever the LLM later picks.

**One rule, one place: planner v2 is live for everyone before Sing All core** (Alan, 2026-11-03; its rollout is handled separately). The quote has to know the target lanes, and it must agree with what the split then produces. So the estimate is built on planner v2's sheet skeleton (`build_sheet_skeleton`) from the start, and Sing All's splits run on v2. There's no interim version on `analyze_score_voice_parts`.

**Depends on planner v2 work:**

| Issue | Gives Sing All | State (2026-11-05) |
|---|---|---|
| SIG-172 | `sheet_lane_counts`, the range in the validator | On `feature/preprocess-planner-v2`, not main |
| SIG-173 | `part_lane_counts`, the job's `laneCounts` | On `feature/preprocess-planner-v2` (cdbf0d7), not main |
| SIG-174 | The LLM question and the `separate` layout | Todo, not built |

All three have to land before step 3 (§16). SIG-174 is needed even though the names won't change: the quote's rows above `shared` get their notes from the `separate` layout, for the recommended voice. Until it lands, `chosen` is always `shared`, so it can never reach `most`. Alan schedules these. The "well under a second" above is a target to measure on real scores.

**When the split doesn't match the estimate:**

| After the split | What happens |
|---|---|
| Same parts as estimated | Continue |
| Fewer parts, because the LLM picked `shared` (`chosen` < `most`, parts made = `chosen`) | Continue, match lanes to rows (below), and refund the unmatched rows' items as "not needed" |
| Fewer parts, because a lane failed (parts made < `chosen`, or no `laneCounts` on the job) | `needs_decision` (§3): review the partial split, continue with the parts that worked |
| More or different parts | Shouldn't happen, because the validator caps lanes at the quoted number. Kept as a safety rule: pause and re-quote. Never charge more than quoted without asking |
| Parts named differently | No pause. Estimated parts map to real split parts by staff and rank (top to bottom), and the real names are shown from then on |

A v2 job writes `laneCounts: [{partId, staff, peak, most, chosen}]` when it completes (SIG-173). `chosen` is the lanes planned, whether or not each one succeeded. v1 jobs don't write it, so they fall into the failed-lane row, which is the safe side.

**Matching real lanes to quoted rows, when `shared` was picked.** Rank doesn't work, because the lane that goes away is usually in the middle:

```python
# rows quoted (separate):  C5/G5/F5   rest/E5/rest   G4/rest/A4
# real lanes (shared):     C5/G5/F5   G4/E5/A4
# by rank, the low lane would get the middle row's voice; it belongs to the bottom row
for lane in real_lanes(staff):                    # top line first
    row = unmatched row with the closest average pitch
    row.lane = lane
    row.voice = row.voice if user_picked(row) else recommended_voice(range_of(lane))
refund every row left unmatched                   # same staff, same length, so the same credits
```

**A score with no lyrics** (from the Part Splitting improvement session, 2026-11-01). Under planner v2, a split of a score with no lyrics fails up front, with the catalog message `preprocess.score_needs_lyrics`. So the estimate also checks for lyrics:
- **No lyrics and nothing to split:** quote as normal. The parts are sung in solfège, as already decided.
- **No lyrics and parts that need splitting:** the quote says "This score has no lyrics, so its shared-staff parts can't be split yet". It offers to sing only the parts that don't need splitting (in solfège), or to add lyrics first. It doesn't start a split that's known to fail.

Every other failed split, e.g. v2's `planner_v2_repair_pending` before its local-repair task lands, arrives as an ordinary failed preprocess job. That goes through the `needs_decision` path above.

**Known limits:**
- A staff with a few divisi bars needs one more lane for the whole song, because `peak` is the minimum. The split really makes that lane, so it's charged like any other. Only lanes above `shared` can come back as a refund.
- The parser's voice labels can differ from the file's tags (one staff gives 4 by the parser, 3 by the tags). That over-reserves but stays safe. The Part Splitting improvement session checks it during the port.


### 5.2 The reconciler

One idempotent `reconcile(jobId)` per parent, run in a Firestore transaction. It runs:
- at "Sing it";
- whenever a child job ends (the child enqueues "reconcile the parent" through Cloud Tasks);
- from the sweeper.

```python
def reconcile(job):
    if job.status in TERMINAL: return
    if job.status == "needs_decision":
        if now > job.decisionDeadline: cancel(job, reason="decision_timeout")
        return
    match job.stage:
        case "reading":   start split children for the parts that need it, else stage = solfege/singing
        case "splitting":
            if any split child running: return
            settle each successful split part; refund failed / review-required parts
            if every needed part split OK: advance()
            else: job.status = "needs_decision"; job.decisionDeadline = now + 24h; email("action needed")
        case "solfege":   run add_solfege (deterministic, ~1 s); on failure:
                            lyrics also chosen -> refund solfege takes and mixes, mode = lyrics, note it, advance()
                            solfege only        -> needs_decision (as above)
        case "singing":   start one synthesis child per take (all at once; GPU queue limits concurrency)
                          each finished take is added to the player record as it ends (§4)
                          when all ended: settle made takes, refund failed ones
                          none made -> failed; some -> continue (partial at the end); all -> advance()
        case "mixing":    render the full mix(es) and part-dominant mixes from the takes made
                          (skip a mix whose takes are missing, refund it)
        case "packaging": build the zip (§9), then
                          status = completed | partial; clear users.activeSingAllJobId; email()
```

The design rules from the render-all HLD still apply:
- score-changing steps (split, solfège) run one at a time on the version chain;
- every synthesis child carries the parent's quote id, checked by the backend;
- a sweeper reconciles any parent with no progress for N minutes. After a stage-specific limit (§17.7), it fails the stuck items, refunds them and emails.

**Stop** (§7.1) on a parent: set `cancelRequested`. The reconciler stops starting new children and ends as `cancelled`. A render execution stops between takes, and the package job stops at its next step. Takes finished by then, or past their stop point (§7.1), are settled; everything else is refunded. The job card keeps the finished takes playable in the chat. Once the parent has started its final write in packaging, it's past its own stop point, and the stop is discarded.

**Retry failed parts:** a new parent of the same kind, holding only the failed take items, priced and confirmed as a mini quote of those items. It reuses the original choices and voices.

## 6. Credits and pricing

| Item | Unit | Rule |
|---|---|---|
| split | per output part | **Included (0 credits) until split charging is enabled**, at public launch. Then: **1 credit per started 30 s of written-order length, per successful part** (decided by Alan, 2026-11-01), the same unit as singing (see §6.1). Failed or review-required parts refunded; repair attempts inside one job free. Split items exist in the data model from day one, at 0 credits, so enabling the charge is a pricing change plus hooking up settle and refund |
| take | per part per mode | Today's rule: `ceil(sung seconds / 30)` credits, with repeats if chosen |
| instrumental | once per job | Today's rule (1 credit, reused across takes) |
| mix | per file | 1 credit per file (Alan, 2026-11-03) |

### 6.1 Split rate: evidence (2026-11-01)

Source: the replay project's Gemini key, used only for planner replays. Alan's AI Studio total was **£11.98 (≈ $15.2 at $1.27/£)** for 4 full runs of the 19-job replay set: v1 twice, slimmed v1 twice, all with prod model settings. That's 76 job runs. The total includes calls that timed out, which the replay logs record as 0 tokens.

| Measure | Value |
|---|---|
| Job runs that produced split parts | 54 of 76 (parts per job: median 2, range 2–3) |
| Score length, written order | median 53 s (4 s to 896 s; the 896 s score never split successfully) |
| Successful part-seconds, 4 runs | 7,142 s (no single score dominates: the top one is 22%) |
| Gemini cost per **successful** job | £11.98 / 54 = **£0.22 ≈ $0.28** (failed runs' cost is carried by the successful ones) |
| Gemini cost per 30 s of successful part | £11.98 / 7,142 × 30 = **£0.050 ≈ $0.064** |

Revenue per credit is $0.138 on the cheapest plan (Pro annual, Founding Offer) and $0.233 on Solo monthly.

| Rule | Typical job (2 parts × 53 s) | Revenue at $0.138 per credit | Covers cost ($0.28 per job)? |
|---|---|---|---|
| 1 credit per started 60 s per part | 2 credits | $0.28 | Only just, on the cheapest plan; no room for higher prod volume or for price changes |
| **1 credit per started 30 s per part** | **4 credits** | **$0.55** | **Yes, about 2× on the cheapest plan, about 3.4× on Solo monthly** |
| Shortest score (4 s, 2 parts), 30 s rule | 2 credits | $0.28 | Yes, about break-even on the cheapest plan |

**Decided (Alan, 2026-11-01): 1 credit per started 30 s, per successful part.** Example: a 4-part, 3-minute song costs 4 × ceil(180/30) = 24 credits to split, the same as singing those 4 parts. Revisit (lower) once planner v2 cuts the Gemini cost. It's the same unit and rounding as singing, so it's easy to explain. When planner v2 cuts Gemini cost, this can be lowered later; prices are easier to cut than to raise.

**Reserve, settle, refund:**
- **Reserve.** At "Sing it", one transaction checks the balance against the quote total, writes one reservation per item, and sets `users.activeSingAllJobId`. Today's `reserve_credits` reserves per job, so this extends it to reserve per item, all under one parent.
- **Settle.** Each item settles on its real duration, exactly as a synthesis job does today (`settle_credits`). A failed item is released (`release_credits`).
- **Receipt.** The final card and the email show each item's reserved, charged and refunded credits.

**Chat splits get charged too, from public launch (§16, step 8).** "Split the soprano and alto" in chat must then:
1. present a quote (split parts × duration);
2. get the user's confirmation in a later turn, under the same consent rule as synthesis (`quote_presentation`, active quote, presented turn);
3. reserve, then settle per successful part.

This changes today's preprocess flow, which starts without a price. It's the biggest change to existing behaviour in this design, so it's built on planner v2 once that's live, and tested on the allowlist before launch (§16, step 8).

**Contract Sing All relies on:** Sing All starts preprocess jobs and reads their result: the job's status, whether review is required, and the derived parts in the new score version. Planner v2 keeps that contract (the plan format, lint and postflight are unchanged). It also writes `laneCounts` (SIG-173), so Sing All can tell a lane it chose not to use from a lane that failed (§5.1). Any change to it must be flagged to Sing All first.

## 7. Concurrency and what's blocked

```text
"Sing it":   transaction: if users.activeSingAllJobId is set and that job isn't terminal -> refuse
                          ("A Sing All is already running", with a link)
             else set it, reserve, create the parent
job ends:    transaction: clear users.activeSingAllJobId
while active, refuse (backend, not just UI):
   - in the job's Song: score-changing tools (split, solfège, edits), any synthesis
     Stop and Replace are always allowed (§7.1)
   - in any Song: "Sing it" (a second Sing All)
   allowed: chat in any Song; in other Songs, uploads, edits, splits and single takes; listening; downloading
```

Single takes in other Songs don't compete for the RTX, because they run on the L4 (§17.5). Credits stay safe: the Sing All's quote is reserved up front, and every other job reserves its own against what remains. A take refused for low balance shows the usual message.

Each Song keeps today's rule for chat: one job at a time. The UI holds the chat turn until the job ends, so a second request can't start from the chat.

The guard lives in Firestore, so it holds across API instances. That also closes the known gap where the per-session chat lock only works within one instance.

### 7.1 Stop: one way to stop work, app-wide

The product has no way to stop work today. This adds one, for everything, not as a Sing All feature.

**In the chat:** a Stop button sits next to Send whenever anything is running in this Song. During a chat reply, split or take, Send is unavailable, as today, so Stop is the one action. During a Sing All, chat stays available, so both work. Sing All asks for confirmation first, because it can be many credits in; the rest stop on one tap.

**What it stops:** normally there is only one thing running in a Song. The function still loops over all active jobs in the Song, because the backend alone allows more: a second tab, or a job left running after the page lost its connection.

**The stop point** (Alan, 2026-11-03): each job has one point after which a stop is too late. Before it, the job can be stopped. After it, the stop or replace is discarded and everything on completion happens as if it was never clicked: the audio or score is published, and credits are charged. Because the worker and the Stop request both go through one transaction, the user gets a definite answer straight away, and a finished result is never thrown away.

```python
def stop_song_work(session):                 # POST /sessions/{id}/stop, also used by Replace
    session.stopTurnRequested = current turn, if any
    results = [stop_job(job) for job in jobs(session) where status in {queued, running}]
    add to chat history: what stopped, and what was charged or refunded    # so the LLM knows too
    return too_late if all(r == too_late for r in results) and results else stopped

def stop_job(job):
    in one transaction:
        if job.pastStopPoint or job has finished: return too_late     # nothing changes
        job.status = "cancelled"
        release its reservation              # Sing All: settled and refunded per item (§5.2)
        return stopped

def pass_stop_point(job):                    # the worker's last check
    in one transaction:
        if job.status == "cancelled": return False       # stop here
        job.pastStopPoint = True
        return True                          # from here it always completes
```

| Work | Its stop point | Before it, Stop does | Credits and our cost |
|---|---|---|---|
| Chat reply | Reply saved | Ends before the next LLM call or tool; the user's message stays, with no reply | None. A call in progress is paid for |
| Add solfège | A tool inside a chat turn (about 1 s), not a job, so it follows the chat reply | Same as a chat reply. If it has already saved, that version stays | None |
| Split | Just before the result is saved as a new score version | Ends within about 30 s; the score is unchanged | Free today; refunded once splits are charged. A Gemini call in progress is paid for |
| Single take | Just before the vocoder, the last stage | The take is dropped and no audio is published | Full refund. A stage in progress runs to its end, and we pay for it |
| Export mix | Just before the final write | The mix is dropped | Full refund |
| Sing All | Packaging's final write. Each take also has its own stop point | Stops at the next take or step | Finished takes and takes past their stop point are charged; the rest are refunded |

A retry after a worker dies clears `pastStopPoint`, because the job goes back to the queue.

**How the running work notices:**

```python
# Worker, once it has a GPU slot, before sending "synthesize" to the GPU process
if job.status == "cancelled": close the run; no render

# GPU process, in synthesize()
align → durations → pitch → variance → acoustic → vocoder
before each of the first five: if job.status == "cancelled": return {"status": "stopped"}
before vocoder:                if not pass_stop_point(job):  return {"status": "stopped"}

worker heartbeat (every 30 s):  if job is no longer running: cancel this run; no retry
any retry from the queue:       finished job, so it's skipped (already true today)
chat turn loop:                 before each LLM call and tool call: if stopTurnRequested: end the turn
split, before saving:           if not pass_stop_point(job): discard
completion transaction:         if job.status == "cancelled": discard      # safety net, e.g. an old worker during a deploy
result of a split or solfège:   if job.scoreId != session.scoreId: discard
result of a take:               if take.scoreId != player.scoreId: don't add it to the tracks
```

- **`stopped` is a normal result, not an error.** The MCP router restarts a process when a call fails, which would reload its models and disturb other takes.
- **Reading the status:** the GPU process already has the job store, for progress updates. Each check is one Firestore read, about 6 per take.

**What the user sees:** "Stopped", with what was charged or refunded; or "Too late to stop: it's just finishing", and then the normal result.

**New behaviour this needs in today's code:**
- A new `cancelled` status, which the progress API and the UI treat as a finished state, and `pastStopPoint` on the job.
- Today the heartbeat only logs when the lease is lost and the run carries on. It has to cancel the run.
- Shutdown cancels runs too and hands them back for a retry, so a run has to tell a Stop apart from a shutdown.
- Status checks before the render and before each `synthesize` stage, and a `stopped` result the worker handles without a restart or retry.

## 8. Progress, refresh and re-login

- The UI reads the active job from `GET /sing-all/active`, which returns `users.activeSingAllJobId` plus its progress. It polls while running, the way today's job progress works (`build_progress_payload`).
- On page load, after login, or on any device, the UI asks for the active job. If one exists, the header pill shows it from any Song, with a link to its Song, and the live card appears in that Song's chat.
- Progress payload: status, stage, per-item status (a split part, take or mix: queued, running, done, failed), elapsed time, and a rough remaining estimate from item durations.

## 9. Results: zip, server mixes, retention

**No snapshot copy** (Alan, 2026-11-03). The session, its score versions and its take files already last: in prod, Firestore has no TTL policy and the Storage bucket has no lifecycle rule, so nothing is deleted, and `FirestoreSessionStore` doesn't enforce `BACKEND_SESSION_TTL_SECONDS`. Only the local in-memory store does. The player record points at those files, and the job keeps `finalScoreVersion`, `zipPath` and `summary` for its receipt.

**Retention:**
- **Paid:** kept indefinitely.
- **Free:** a free user's share link stops 30 days after the owner's last change to that song. Free plans can't run Sing All for now, so they have no zips.
- A deleted Sing All job removes its zip and mixes; the billing record stays.

**Zip**, built in the `packaging` stage and stored at `singAll/{uid}/{jobId}/{title} - Sing All.zip`. Contents:
- `1 Solo vocals/` (WAV per take)
- `2 Part-dominant mixes/`
- `3 Full mixes/` (MP3)
- `4 Instrumentals/` (MIDI)
- `5 Voicebank credits/` (`character.txt`)
- `README.txt`

Downloads use short-lived signed URLs.

**Audio formats:** chat takes stay MP3, the prod default, which plays in every major browser. **Sing All takes keep a WAV as well, for now, for troubleshooting** (Alan, 2026-11-03): the WAV goes in the zip and feeds the server mixes, and the MP3 goes to the player and share page. Mixing from WAV also avoids the small delay MP3 encoding adds at the start of a file, which could misalign tracks.

### 9.1 Server mixes: instruments and vocals, balanced automatically

Today the backend can only mix vocal takes (`audio_mix.render_mix_to_wav`), and instruments are only synthesised in the browser. Sing All needs both mixed on the server, with no owner present to set levels.

**Rendering instruments:** FluidSynth on the `sing-all-package` CPU job, with the same FluidR3 soundfont as the browser player. It is a widely used command-line renderer, runs faster than real time and writes a WAV. Parts the player leaves muted by default (a drum part with no kit chosen) are removed from the MIDI first. The repeat setting picks the matching MIDI file, and the sample rate is set to the takes' rate.

**Alignment:** the player starts every take and the MIDI together at time 0 with no offset, so the server mix does the same.

**Principles:**
- **Measure loudness, not peaks.** Loudness (ITU-R BS.1770, in LUFS) follows what the ear hears. Its gating ignores silence, so a part with long rests isn't misread as quiet.
- **One fixed gain per track for the whole song.** The music's own dynamics stay intact.
- **Vocals are levelled against each other,** because voicebanks differ in level and no voice should vanish.
- **Instruments are moved as one group.** The MIDI's own balance between them is part of the arrangement; levelling them separately would make a quiet triangle as loud as the piano.
- **Instruments are measured only where someone is singing,** because that's where they can cover the voice. A loud piano intro shouldn't push the piano down under the verses.

```python
VOCAL_REF_LUFS               = -23
FULL_INSTRUMENTS_DB          = -6     # instruments relative to the vocals
PART_DOMINANT_INSTRUMENTS_DB = -12
TARGET_LUFS                  = -16    # every mix file ends up equally loud
PEAK_LIMIT_DB                = -1
MAX_LIMITING_DB              = 3
MAX_VOCAL_GAIN_DB            = 12;  MAX_INSTRUMENT_GAIN_DB = 18   # a near-silent stem can't be blown up into noise

def lufs(audio):                      # measured in stereo: mono vocals are copied to both channels first
    return BS.1770 integrated loudness, or None if it's silent or too short to measure

def balanced_mix(vocals, instruments, instruments_db):
    # 1. Level each voice to the same reference. A silent take is dropped.
    for v in vocals:
        v.gain = clamp(VOCAL_REF_LUFS - lufs(v), MAX_VOCAL_GAIN_DB)
    vocal_bus = sum(v * v.gain for v in vocals)

    # 2. Place the instruments relative to the voices, measured where the voices sing.
    mix = vocal_bus
    if instruments:
        sung = parts of the song where vocal_bus is above -50 dBFS
        measured = lufs(instruments[sung]) if sung lasts at least 3 s else lufs(instruments)
        if measured is not None:
            gain = clamp((lufs(vocal_bus) + instruments_db) - measured, MAX_INSTRUMENT_GAIN_DB)
            mix = vocal_bus + instruments * gain

    # 3. Bring the whole mix to the target, and catch the peaks.
    gain = TARGET_LUFS - lufs(mix)
    overshoot = peak_db(mix * gain) - PEAK_LIMIT_DB
    if overshoot > MAX_LIMITING_DB:           # a limiter working this hard would be audible
        gain -= overshoot - MAX_LIMITING_DB   # so accept a quieter file
    return limit(mix * gain, PEAK_LIMIT_DB)   # replaces today's hard clipping

for mode in job.modes:                        # lyrics and solfège are mixed separately
    full_mix = balanced_mix(all_vocals(mode), instruments, FULL_INSTRUMENTS_DB)
    for part in parts:                        # other vocal parts left out
        part_dominant[part] = balanced_mix([vocal(part, mode)], instruments, PART_DOMINANT_INSTRUMENTS_DB)
```

**Worked example:** soprano, alto, tenor and bass with piano. The figures are illustrative, not measured.

| Track | Measured | Step 1 gain | After |
|---|---|---|---|
| Soprano | -19 LUFS | -4 dB | -23 |
| Alto | -22 | -1 dB | -23 |
| Tenor | -24 | +1 dB | -23 |
| Bass | -26 | +3 dB | -23 |
| Four voices together | | | about -17 |

| | Full mix | Soprano-dominant mix |
|---|---|---|
| Vocals before step 3 | about -17 | -23 (soprano only) |
| Piano target | -17 - 6 = -23 | -23 - 12 = -35 |
| Piano gain (it measures -20 where sung) | -3 dB | -15 dB |
| Whole-mix gain to reach -16 | about 0 dB | about +7 dB |
| Vocals in the final file | about -17 | about -16 |
| Piano in the final file | about -23 | about -28 |

These mixes use the score's default instruments and are fixed when the job ends; the owner's later player changes don't alter them.

**The player opens with the same balance** (Alan, 2026-11-03), so the first listen matches the zip:

```python
when a Sing All's mixes are done, unless the owner changed the mix during the job:
    gains = the full mix's step 1 and step 2 gains, for the mode shown first
    shift = max(gains)                                 # the player's track volume tops out at 1
    each vocal.volume = db_to_gain(vocal.gain - shift)
    each instrument.volume = 1;  bus.levelDb = instruments.gain - shift
```

It's approximate, because the browser's instruments and FluidSynth's differ a little. The share page follows, as it reads the same record.

**For troubleshooting:** the job summary records each stem's measured loudness and gain, and each mix's final loudness, peak and limiting.

**Library:** `pyloudnorm` (new dependency) measures loudness; `ffmpeg`, already in use, applies the limiter.

**Before launch:** the browser and FluidSynth can sound a little different, so the two instrument offsets and the instrument sound need a listening check on real songs.

## 10. Notifications (email)

- **Provider:** Brevo transactional (new; Brevo is used today only for marketing contacts). English only. From a real reply-to address.
- **When:** on entering completed, partial, needs_decision or failed, and on the decision-timeout cancel. Sent once per status (`emailSentFor`). Sent even if the user watched it finish.
- **Content:** the song title, the outcome in one line, the receipt, and **Open your song** → `https://sightsinger.app/jobs/{jobId}`.
- **The link is owner-only:** it requires sign-in; anyone else sees "This job isn't yours. Ask the owner for a share link".

## 11. Reopening a Song (owner)

**Today a reload loses the whole chat session, not only the mixer:** the app keeps the session ID only in memory, creates a new session on load, and has no API to read a session back. The data stays on the server, but nothing can open it again. Songs, Sing All's job link, the saved mix and a live share link all need this, so it is built once:

```text
GET /sessions/{id}/workspace (owner only) returns:
   chat history, score version, score summary, player record, active job if any

/jobs/{jobId} → sign in if needed → owner check → open the job's Song
   chat: the history, with the job summary as the latest assistant message
   if the Song's score was since replaced: the job card and zip remain, and Retry is refused
   LLM context on the next turn: the summary of the current score version + the job summary
       (parts, voices, mode, verse, repeats, what failed), the same context it would have had online
   the user can carry on: edits create new score versions in this chat; re-singing is a new quote

/app/song/{id} → sign in if needed → owner check → open the Song the same way; a reload lands here
/app            → no Song open: upload box, demo songs, My songs
```

**Lyrics / solfège toggle** (job has both): a pill above the score preview.
- **Lyrics:** shows the verse lyrics and loads the lyrics takes into the player.
- **Solfège:** shows the solfège syllables and loads the solfège takes.

The player keeps each part's volume and mute settings across the switch.

## 12. Future free preview: kept possible, not built

- Every take item has `range` (null = the whole song).
- The parent has `kind`.
- Credits are worked out per sung second, per item.

A preview job is `kind = preview` with takes of `range = 0–30 s`. Both of the options Alan described stay possible:
1. **Charge the full run again.** No change needed.
2. **Don't double-charge.** Price the full run's takes from `range = 30–end`, plus a later concatenation step.

Nothing here assumes one take equals the whole song.

## 13. My songs

- The drawer lists all the user's Songs, newest activity first: title, date, status, parts, shared or not. A Song with Sing All jobs also shows their receipts and zips.
- Each row has Open (§11), Share (§14) and Delete.
- A Song with a running Sing All is pinned at the top with its live status.
- Delete asks for confirmation and isn't available while a job runs. It removes the Song from the list, its link, zips and mixes; the billing record stays.

## 14. Share (public, read-only)

Works for any Song with sung takes, on free and paid plans.

**Creating a link:**
- Share sits near the score preview, in the app and on the share page.
- The first share shows a one-time notice: anyone with the link can listen, and will see later changes to this song; check you have the rights to the score; voicebank terms apply.
- Then `POST /sessions/{id}/share` creates (or returns) a random, unguessable token and a `shares/{token}` record.
- The link is **`https://sightsinger.app/s/{token}`**, and it always returns the same link for the same song.
- Turning sharing off sets `enabled = false`, and the link then shows "This link was turned off".

**A live link:** there is no published copy. Each page load builds the page from the player record, so it always shows the owner's latest score, tracks and saved mix. Viewers aren't pushed changes; they refresh.

```python
GET /public/s/{token}:
    share = shares[token];  if missing or not enabled: "This link was turned off"
    p = player(share.sessionId)                  # never touches lastActiveAt
    rows = flatten(usable_tracks(p) + p.instruments, p.bus)
    return title, credits, modes, defaultMode, measureMaps, rows, isOwner

def flatten(vocals, instruments, bus):
    instruments = [i for i in instruments if not i.unassigned]   # a kit never chosen is not a track
    any_inst_solo = any(i.solo for i in instruments)
    for i in instruments:                                        # same rule as the player
        i.muted = bus.muted or i.muted
        i.solo = i.solo if any_inst_solo else bus.solo
        i.gain = i.volume * db_to_gain(bus.levelDb)
    for v in vocals: v.gain = v.volume
    rows = vocals + instruments
    any_solo = any(r.solo for r in rows)
    for r in rows: r.defaultOn = r.solo if any_solo else not r.muted   # same rule as export mix
    return rows
```

| Owner's setting | On the share page |
|---|---|
| Volume, bus level | The track's fixed level; no fader |
| Mute | Starts off; the viewer can turn it on |
| Solo | The soloed tracks start on, the rest off; the viewer can change that |
| Instrument choice | The MIDI plays with the owner's instrument; no selector |
| Lyrics or solfège | The starting mode; the switch stays if the song has both |

The viewer's switches only change on/off in their own browser. Nothing is saved, and a reload returns to the owner's mix.

**What follows from a live link:**
- **A take sung from music that has since changed** is left out, the same rule as the owner's player. If the owner edits a part and hasn't re-sung it, viewers don't hear that part until they have.
- **Sing All running on a shared song:** a refresh shows the tracks made so far.
- **Replacing the score:** the same link shows the new score, with no audio until it's sung.
- **The score, takes and MIDI** are read from the Song's existing files.

**The share page:**
- **No sign-in;** a Sign in button is at the top.
- A signed-in viewer still gets the read-only page. **The owner also sees "Open in SightSinger"**, which opens the Song to change the mix (§11).
- **Shows:** the score preview of the current version, the transport (play, pause, seek), master volume, and a mute/unmute switch per part. It also shows a lyrics/solfège switch if the song has both, and the voicebank credits.
- **Does not show:** chat, the instrument selection section, any editing, or downloads.
- It also has its own Share button, which copies the same link.
- **Playback:** the same browser engine as the owner's player: decoded takes plus the MIDI through FluidR3. Songs with instruments download the **142 MB** soundfont on a viewer's first play, then the browser's cache serves it; songs without instruments don't load it. The page shows the loading progress. This needs a test on a real phone before launch, for download time and memory.

**Serving it safely:**
- Score, MIDI and audio are streamed through the API (`/public/s/{token}/…`), as `/sessions/{id}/audio` does today. A signed Storage URL would show `sessions/{uid}/…`, and the existing playback token carries the user ID in readable form, so neither is used here.
- Rate-limited per IP, including the audio routes, because free users can now publish links too.
- `noindex`, so search engines don't list share pages.
- The design follows the earlier read-only score-player artifact.

## 15. APIs (new)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/sing-all/quote` | user | The parts estimate (§5.1) plus the quote from the choices (used by quick mode and Customise; recalculated live) |
| POST | `/sing-all/jobs` | user | "Sing it": consent check, one-active guard, reserve, create the parent, reconcile |
| GET | `/sing-all/active` | user | The active job and its progress (restores the card after refresh or login) |
| GET | `/sing-all/jobs` | user | Job list |
| GET | `/sing-all/jobs/{id}` | owner | Status, receipt and links to the session and zip |
| GET | `/sessions/{id}/workspace` | owner | Open a Song (§11) |
| GET | `/songs` | user | My songs |
| PATCH/DELETE | `/songs/{id}` | owner | Rename or delete a Song |
| POST | `/sessions/{id}/stop` | owner | Stop all work in this Song (§7.1) |
| POST | `/sing-all/jobs/{id}/decision` | owner | needs_decision → continue with the parts that worked (returns a new quote) or cancel |
| POST | `/sing-all/jobs/{id}/retry` | owner | Quote and run the failed takes again |
| PUT | `/sessions/{id}/player/mix` | owner | Save the mix and instrument choices |
| POST/DELETE | `/sessions/{id}/share` | owner | Create or return the link / turn it off |
| GET | `/sing-all/jobs/{id}/download` | owner | Signed zip URL |
| GET | `/public/s/{token}` | none | The share page's data, built from the player record |
| GET | `/public/s/{token}/score`, `/midi`, `/audio/{trackKey}` | none | Streamed score, MIDI and take audio |

Internal: `POST /internal/sing-all/reconcile` (Cloud Tasks) and the sweeper (Cloud Scheduler).

## 16. Rollout

Agreed with Alan, 2026-11-03. Each step ships on its own, and Alan deploys and merges.

| # | What ships | Needs | Who sees it |
|---|---|---|---|
| 0 | **Planner v2**, live for everyone (handled on its own thread) | Its own replay gate | Everyone |
| 1 | **Stop** for all work (§7.1): button, `cancelled` status, stop point, worker and stage checks, upload refusal | 0 | Everyone |
| 2 | **Songs** (§2.1, §4, §11, §13): `/app/song/{id}`, title, New song, the "New song or replace?" popup, My songs, player record, saved mix and instruments, `GET /sessions/{id}/workspace` | 1 | Everyone |
| 3 | **Sing All core** (§5, §7, §8): deploy the `sing-all-reconcile` queue and the `sing-all-render` RTX Cloud Run Job with render-slot admission (§17); quote and parts estimate on `part_lane_counts`, which needs SIG-172, SIG-173 and SIG-174 first (§5.1); parent job, reconciler, one-active guard, per-item reserve and settle (split items at 0 credits), refund of lanes not used; splitting, solfège and singing stages; progress API, in-chat live card, header pill; Stop for the parent; sweeper; **premium RTX routing off in this release** | 1, 2 | Allowlist |
| 4 | **Server mixes and zip** (§9, §9.1), starting with the listening trial on real songs: FluidSynth, balance, `sing-all-package` CPU job, WAVs, the player's opening balance | 3 | Allowlist |
| 5 | **Emails and job link** (§10, §11) | 3 | Allowlist |
| 6 | **Split or solfège failures** (§3, §5): needs_decision, review a partial split, re-quote on mismatch, retry failed parts | 3 | Allowlist |
| 7 | **Share link and public page** (§14), held back until launch so the launch leads with Sing All | 2 | Allowlist |
| 8 | **Split charging, ready but off for the public** (§6): chat splits on the job model with a quote and consent step; Sing All split items priced; rate set from v2's prod cost; free-link expiry | 0, 3 | Allowlist |
| 9 | **Public launch** for paid users: Sing All, sharing and split charging together | 4, 5, 6, 7, 8 | Everyone |

**The allowlist** is the existing maintenance allowlist that premium routing uses (`app_config/maintenance`), so VIP users can be testers.

**Before public launch:**
- Listening check of the server mixes on real songs (§9.1).
- The share page on a real phone, for the 142 MB soundfont's download time and memory (§14).
- The real cost per job from billing, against §17.6.
- The split rate, set from planner v2's prod cost (§6).

**How this maps to unifying chat and Sing All.** A single take is a Sing All with one item at the data level, but chat and Sing All differ in policy: who starts the work, where it runs, how many run at once, and what happens on failure.

| Stage | Steps | What |
|---|---|---|
| 1 (approved) | 1–3 | One player record for chat and Sing All. A job record general enough for other kinds (`kind`, items). Sing All calls the existing credit and job functions rather than copying them. Chat gets additive changes (record each finished take, save the mix) plus Stop, which changes how chat jobs end, including settlement. Quote and consent logic for takes are untouched |
| 2 (approved) | 8 | Chat splits move onto the job model, with split charging |
| 3 (not approved) | None | Chat takes move onto the job model, behind a flag. It's the main revenue path, so it would go last and only after Sing All is stable in prod |

**Later:** free 30-second preview (§12); a single preprocess job planning all parts (render-all HLD phase D).

## 17. Deployment architecture

### 17.1 Principles

1. **Sing All is asynchronous.** The user can leave, and an email arrives. So it can trade a minute of start-up for **no idle cost**. Interactive work keeps its low-latency path.
2. **Pay for GPU only while it sings.** No always-on GPU (min 1 RTX instance ≈ $2,300/month), and no 5–10 minute idle tail after each burst.
3. **Scale on what is actually scarce:** RTX quota (1 GPU in us-central1 today) and Gemini time for splits, not API instances.
4. **Reuse what's deployed:** the CPU API, the preprocess worker, the queues, Firestore and the bucket. Add only the pieces Sing All needs.

### 17.2 Overview

```text
                 Firebase Hosting (app, /jobs/{id}, /s/{token} share page)
                                   │ HTTPS
                                   ▼
        ┌──────────── CPU API: sightsinger-api (Cloud Run, us-east4) ────────────┐
        │ /sing-all/* APIs · quote + parts estimate · /public/s/{token}           │
        │ /internal/sing-all/reconcile  (the reconciler, short and idempotent)    │
        └───────┬───────────────┬────────────────────┬──────────────────┬────────┘
                │ Cloud Tasks   │ Cloud Tasks        │ Cloud Run Jobs API│ Brevo API
                ▼               ▼                    ▼                   ▼
   preprocess-jobs queue   sing-all-reconcile   ┌─────────────────────┐  transactional
   → preprocess worker     queue (new)          │ sing-all-render     │  email
     (Cloud Run, us-east4,                      │ RTX Cloud Run JOB   │
      Gemini-bound)                             │ us-central1 (new)   │
                                                ├─────────────────────┤
                                                │ sing-all-package    │
                                                │ CPU Cloud Run JOB   │
                                                │ us-east4 (new)      │
                                                └─────────────────────┘
        Firestore (singAllJobs, users.activeSingAllJobId, shares)   ·   Cloud Storage bucket
        Cloud Scheduler: sweeper every 5 min · retention cleanup daily
        Interactive single takes: L4 service, us-east4 (premium RTX routing turned off at launch, §17.5)
```

### 17.3 Components

| Component | Type | Region | Sizing / scaling | Why |
|---|---|---|---|---|
| CPU API `sightsinger-api` | Cloud Run service (existing) | us-east4 | Unchanged (2 vCPU / 4 GiB, ≤10 × 10) | Hosts the Sing All APIs, the reconciler and the public share endpoint. All short requests, so no new service is needed |
| `sing-all-reconcile` queue | Cloud Tasks (new) | us-east4 | Up to 50 concurrent dispatches; retries with backoff | Every child-job end and every stage change enqueues "reconcile parent X" |
| Preprocess worker | Cloud Run service (existing) | us-east4 | Today ≤5 × 4 = 20 concurrent splits; queue of 20 | Splits are mostly waiting on Gemini. To scale, **raise concurrency per instance** (cheap: the CPU is idle while waiting) before adding instances |
| **`sing-all-render`** | **Cloud Run Job on RTX PRO 6000 (new)** | us-central1 (where the RTX quota is) | One execution per parent's singing stage; a process pool of 4 (`MCP_GPU_WORKERS=4`, the benchmark's best); task timeout 30 min; retries 1 | Runs all of one parent's takes in parallel, writes them to the bucket, updates each take in Firestore, then enqueues reconcile. **A job has no idle tail**: billing stops when it ends |
| **`sing-all-package`** | **Cloud Run Job, CPU (new)** | us-east4 | 4 vCPU / 8 GiB; one execution per parent | Renders the instruments from MIDI with FluidSynth, balances and mixes the full and part-dominant mixes (§9.1), encodes the MP3s and writes the zip. CPU work: running it on the RTX would cost about 6× more |
| Firestore | existing | — | — | Parent jobs, the one-active guard, shares, the email outbox |
| Cloud Storage | existing bucket | US | Lifecycle rules (§17.6) | Takes, mixes, zips |
| Cloud Scheduler | new triggers | — | Sweeper every 5 min; cleanup daily | Stuck jobs; free-plan link expiry |
| Brevo | external | — | Transactional API; free tier 300 emails/day | The job emails, sent from an outbox document so they're retried and never sent twice |
| Interactive takes | L4 service | us-east4 | Unchanged | Users waiting on screen keep the fast path. The premium RTX service gets no traffic once routing is off (min instances 0, so it costs nothing) |

### 17.4 Why a Cloud Run Job for the singing stage

| | RTX **service** (today's premium worker) | RTX **job** (proposed for Sing All) |
|---|---|---|
| Start-up | Warm: instant. Cold: about 1 min (voicebank download, models) | Always cold: about 1 min |
| After the last take | Instance kept 5–10 min, **billed** (≈ $0.27–0.53 on the RTX) | Execution ends, billing stops |
| One typical Sing All (8 takes of a 2-min song) | ≈ 1 min start + ~2 min singing + 5–10 min tail ≈ **$0.42–0.69** | ≈ 1 min start + ~2 min singing ≈ **$0.16** |
| Many back-to-back jobs | The tail is shared, so it gets cheaper per job | Each pays its own ~1 min start (≈ $0.05) |
| Fit | Interactive, steady traffic | Asynchronous, bursty traffic: Sing All today |

The extra minute doesn't matter for an asynchronous job, and it's roughly 3–6× cheaper at today's volume.

Cost per Sing All by volume, for a typical 2-minute song (RTX ≈ $0.053 a minute; a job pays ~1 min start + ~2 min singing; a service stays up while the gap between jobs is shorter than its tail, so it pays the whole gap):

| Sing All volume at peak | Gap between jobs | Job | Service | Better |
|---|---|---|---|---|
| Under ~5 an hour | over ~12 min | ≈ $0.16 | ≈ $0.42–0.69 (singing + tail, often a cold start) | Job |
| ~6–15 an hour | ~4–10 min | ≈ $0.16 | ≈ $0.21–0.53 (pays the whole gap) | Job |
| ~20 an hour | ~3 min | ≈ $0.16, but the GPU is always busy and a line builds | ≈ $0.16 | Same cost; the service has about 1.5× the capacity |
| Over ~20 an hour | under 3 min | One GPU can't keep up | ≈ $0.11–0.16, up to ~30 an hour | Service |

Batching (§17.5: one execution takes several waiting parents) shares one start-up across a long line, which pushes the switch point further out. **Revisit** if Sing All volume grows enough to keep an RTX service busy: the dispatcher could then prefer a warm service and fall back to a job (§17.5). Cloud Run jobs already run on the RTX: the GPU benchmark (`sightsinger-gpu-bench`) is one.

### 17.5 Scaling and admission control

```python
MAX_RENDER_SLOTS = rtx_quota_gpus        # 1 today; config, raised when Google grants more quota
on stage "singing":
    if running_render_executions() < MAX_RENDER_SLOTS: start a sing-all-render execution for this parent
    else: parent waits as "queued for singing", with its place in line shown in the UI
on a render execution's end: reconcile the parent, then start the next waiting parent (oldest first)
```

- **Throughput per RTX GPU:** about 3 minutes per typical parent (2-minute song), including start-up, so roughly **20 Sing All jobs an hour per GPU**; longer songs take proportionally longer. More quota scales this linearly. **No extra quota is requested before launch** (Alan, 2026-11-01). Request it when the render line shows it's needed: render-slot wait p90 over 10 minutes on most days of a week (the §17.8 alert tracks this). Google grants no-zonal-redundancy quota more readily.
- **Splitting:** 20 concurrent splits today, each 1–5 minutes, bound by Gemini. Planner v2 is expected to cut that time a lot. Raise preprocess concurrency before adding instances.
- **The API and reconciler** are short requests on the existing autoscaling service; no change needed.
- **Later batching:** one render execution can take several waiting parents, to share one start-up when the line is long.
- **The RTX quota is Sing All's alone:** today's premium worker (allowlisted interactive takes) shares the same 1-GPU quota, and a render execution can't start while that service holds an instance, including its 5–10 min idle tail. So **premium routing is turned off when Sing All launches** (`SYNTHESIS_PREMIUM_ROUTING=off` in `prod.api-cpu.env`; Alan, 2026-11-01). Allowlisted users' single takes go back to the L4. Turn it back on only with more quota.
- **No silent fallback to the L4** when the RTX line is full: the job waits and shows its place. That keeps the earlier decision ("an RTX failure fails the take") and avoids double GPU cost.

### 17.6 Cost efficiency

| Measure | Effect |
|---|---|
| Singing as a Cloud Run **job** | No idle tail: ≈ $0.16 instead of ≈ $0.42–0.69 per occasional Sing All |
| Mixing and zip on a **CPU** job, not the GPU | ≈ $0.01 instead of RTX time |
| A process pool of 4 on the RTX | The benchmark showed 2.1× throughput over 1 take at a time. 8 processes added only 22% more |
| **The player and share page use MP3** | WAVs are only for Sing All's zip and mixes. Audio streams through the API, with rate limits on public routes |
| Rate-limited share page; short-lived signed URLs for zips | No hot-linking, so no runaway egress |
| Storage class | Results use **Autoclass**, which moves rarely played files to cheaper storage on its own. Nothing is deleted automatically in prod today (§9); whether to clean up old free-plan files is a separate decision |
| Splits on the preprocess worker's spare concurrency | Gemini wait doesn't buy CPU instances |
| Budget alerts | Separate alerts on GPU spend and on Gemini spend, and a daily report of GPU-seconds and Gemini cost per Sing All job |

**Estimated cost of one typical Sing All**, assumed to be 4 parts on 2 staves, a 2-minute song, lyrics + solfège (8 takes), full + part-dominant mixes. Song length comes from the daily job reports, 9 Jun–1 Nov 2026, completed takes by non-internal users, longest take per score: paid users' songs median 94 s, mean 106 s, p90 235 s (102 songs); all users median 58 s, mean 83 s (583 songs). Sing All is paid-only, so 2 minutes is the paid median rounded up. This is an estimate: past estimates came out about 2× low, so check against billing after launch.

| Part | Estimate |
|---|---|
| Splitting (2 staves, Gemini, today's planner) | ≈ $1.00 (≈ $0.064 per 30 s of part from the replay evidence, §6.1 × 4 parts × 2 min); planner v2 should cut this a lot |
| Singing (RTX job, ~3 min) | ≈ $0.16 |
| Mixing + zip (CPU job, ~2–3 min) | ≈ $0.02 |
| Storage (≈ 200 MB, with WAVs in the zip) | ≈ $0.005 a month |
| Email, Firestore, Tasks | ≈ $0 |
| **Total** | **≈ $1.20 (allow up to ≈ $2.40)**, against ≈ 37 credits (8 takes × 4 + 5 mix files at the prototype's 1 each) ≈ $5.10 revenue on the cheapest plan, with splits free for now |

### 17.7 Reliability

- **Everything is idempotent and resumable.** Reconcile runs in a transaction. A render execution skips takes that already exist, so a retried execution only does what's missing.
- **Lost work is caught by the sweeper:** a parent with no progress past its stage limit is reconciled, then stuck items are failed, refunded and emailed. Limits (Alan, 2026-11-03):

| Stage | Limit | Clock | Why |
|---|---|---|---|
| Split | 30 min per split child | Restarts for each child and each dispatch retry | Each attempt can take 5 min and a split can make 3 of them, plus planning. The worker's own request timeout is 30 min, so past that the run is certainly dead. The longest in the reports is about 12 min (94 jobs, median about 4 min) |
| Singing | 20 min with no new take finished | Starts when the render execution starts; waiting in line for the GPU doesn't count | |
| Mixing and packaging | 10 min | Starts when the package job starts | |

- **Each render execution has a hard timeout** (30 min), then one retry, then its remaining takes fail and are refunded.
- **Region split:** the API and queues are in us-east4; the RTX job is in us-central1, where the quota is. Only small Firestore writes and the take files cross regions (≈ 80 MB per job, negligible). If an RTX quota in us-east4 becomes available, move the job there.

### 17.8 Monitoring

- **Per stage:** count, duration (p50/p90) and failure rate. Also queue depth and wait time for render slots.
- **Per job:** GPU-seconds, Gemini tokens and cost, credits reserved, charged and refunded.
- **Alerts:** a stuck-job count above zero for 15 minutes; render-slot wait over 10 minutes; daily GPU or Gemini spend over budget; email send failures.

## 18. Still to decide

Decided (Alan, 2026-11-01 to 2026-11-03):

- **RTX quota:** no request before launch; request more when needed (§17.5).
- **Routing:** every Sing All job sings on the RTX batch job (`sing-all-render`). Interactive single takes go to the L4: premium RTX routing is turned off at launch, so the one RTX GPU is free for Sing All (§17.5).
- **Rollout order, allowlist and split charging at public launch** (§16).
- **Lanes in the quote:** reserve the worst case per staff, the LLM answers "shared" or "separate" at split time only where the two differ, refund lanes not used (§5.1).
- **Mix rate:** 1 credit per mix file.
- **Player's default balance:** Sing All's player opens with the server mix's balance (§9.1).
- **Stuck-job limits:** split 30 min per child, singing 20 min with no new take, mixing and packaging 10 min (§17.7).
- **Streaming audio format:** MP3, the prod default, which plays in every major browser (§9).
- **Solo vocals in the zip:** real WAVs for now, for troubleshooting (§9).
- **Free share links:** stop 30 days after the owner's last change (§9).
- **Stop touching how chat jobs end:** approved, including settlement and the synthesis stages (§7.1).
- **Sing All takes finished before a Stop or Replace:** charged, and still playable on the job card.
- **A stop or replace that's too late:** discarded; the job completes as if it was never clicked (§7.1).
- **Share page mix:** a live link, refreshed by the viewer; no frozen versions (§14).
- **Share page instruments:** the browser engine, the same as the owner's player (§14).
- **Songs:** one chat and one current score per Song; created at the first upload; every Song is listed in My songs (§2.1).
- **Upload into a Song that has a score:** ask "New song or replace?"; replace does the same stop as the Stop button, then resets the tracks, as today (§2.1, §7.1).
- **Stop:** app-wide, with a button in the chat (§7.1).
- **Singing during a Sing All:** allowed in other Songs; only a second Sing All is refused (§7).

Still open:

- **Stopping inside a stage:** not planned. ONNX Runtime has a terminate option that would need testing on our GPUs. Revisit if billing shows stopped takes are a real cost.
- **Mix tuning:** the two instrument offsets and the FluidSynth sound, by listening to real songs (§9.1). A trial on one real song is planned for later.
- **Singing on a job or a service:** start with the Cloud Run Job (§17.4). Revisit when Sing All averages about 15–20 jobs an hour at peak for a week, i.e. the render line keeps one GPU busy (see §17.4's volume table), or if the 1-minute start-up becomes a user-facing problem (for example a future on-screen free preview).
