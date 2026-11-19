# Sing All step 4: server mixes and the zip — task specs (draft)

2026-11-19. Nothing here is built. Alan answered the step 4 questions on 2026-11-19 (see "Decisions" at the end). The API contract is fixed. No questions are open.

Design: `docs/specs/sing-all-process-backend-hld.md` §6 (mix items), §9, §9.1, §16 row 4, §17.3, §17.6 and §17.7, on branch `docs/sing-all-process-hld`. Steps 1 (Stop), 2 (Songs), 3 (Sing All core) and 7 (Share) are built; step 3 is on `feature/sing-all-core` and `claude/sing-all-ui`, Share on `feature/share`.

Step 4 adds what a Sing All leaves behind besides the takes in the player:

- **mix files** made on the server: per mode, one full mix and one part-dominant mix per sung part, balanced by loudness with no owner present;
- **the zip**: solo vocals (WAV), the mixes (MP3), the instrument MIDI, the voice credits and a README, kept with the job;
- **Download zip** on the finished card;
- **the player opens with the full mix's balance**, so the first listen matches the zip.

It starts with a **listening trial** (HLD §16 row 4): the mixing code is built first, with a trial command Claude runs on real songs; Alan listens, and the numbers are tuned by ear before the rest is wired in.

The repository is public. Tests, fixtures and commits use made-up titles, part names and lyrics only, and no real job or session ids.

## Differences from the HLD

| # | HLD | This spec | Why |
|---|---|---|---|
| 1 | Mixing and packaging are two stages, `mixing` then `packaging` | One stage, `packaging`, run by one package run that mixes and zips | Both run in the same process on the same files. Two stages would be two hand-overs with nothing between them. The card still says "Mixing" and then "Packing the zip" from the run's own progress |
| 2 | Each mix file is an item at 1 credit | Each mix file is an item at 1 credit, as the HLD says, but all of them share **one reservation**, `{jobId}-mixes`. The package run settles it for the files it made and refunds the rest | Mix files are made together by one run. One reservation per file would be up to 2 × (parts + 1) reservations made and settled in one go, with nothing gained |
| 3 | `choices.mixes` exists; what it holds is not said | `choices.mixes`: `full` (full mixes only), `all` (full and part-dominant, the default) or `none`. Customise shows it as a choice; the quote prices the files | The user decides what they pay for. `all` is what the HLD's zip lists |
| 4 | Not covered: the package run fails | The job's status still follows the takes (completed, partial, failed). The mixes reservation is refunded, the card says "The mixes couldn't be made. Your parts are in the player." and there is no zip | The takes are the user's main result and are already in the player. A failed zip must not turn a good job into a failed one |
| 5 | Sing All takes keep a WAV beside the MP3 | Already true for every take: the synthesis save step stores a `.source.wav` and records `losslessOutputPath` on the job (chat and Sing All, inline and the premium worker; the export mix reads it today). No task needed | Found 2026-11-19 while writing M3, which is dropped. The package run reads `losslessOutputPath`, else `outputPath` when the output is itself a WAV, as `main.py`'s export mix does |
| 6 | Not covered: Stop during packaging | Stop during `packaging` is too late: the run finishes, as a take past its stop point does | The takes are all made; the run takes about a minute. Refunding half-made mixes adds a path with little use |
| 7 | "Instruments: the score's default instruments" | The sounds the score summary holds when packaging starts: the score's own, plus those the quote chose and "Sing it" saved (step 3, difference 6). A part with no sound chosen is left out, as the player keeps it muted | That is what the player plays by default, so the zip and the first listen agree |
| 8 | "A deleted Sing All job removes its zip and mixes" | Nothing is removed. Deleting a Song stays a logical delete, as it is today (`deletedAt` set, the Song taken off the list); its jobs, takes, zips and mixes stay where they are | Alan, 2026-11-19: no records are deleted |

## The mixing rule

From HLD §9.1, unchanged except for the numbers the trial may tune. One place: `src/backend/sing_all_mix.py`.

**How the server mixes.** No browser and no real-time playback: the mix is an offline render in Python, on files.

```text
takes (WAV per part and mode) ──read──► numpy arrays ──pyloudnorm: measure, gain──┐
                                                                                    ├─► sum ─► ffmpeg: limit, encode MP3
score MIDI ──mido: events──► pyfluidsynth (in process, FluidR3_GM.sf2) ──► instruments array ──measure, gain──┘
```

- The instruments are rendered with **pyfluidsynth** (Alan, 2026-11-19), the Python binding of the FluidSynth library: no command line, no WAV file in between. `mido` reads the MIDI's events; the code sends each one to the synth (`program_change`, `noteon`, `noteoff`, `cc`, `pitch_bend`) and pulls the samples between events with `get_samples`, at the takes' sample rate, into a numpy array. Tracks of parts with no sound chosen are skipped in that loop. It is the only step that turns MIDI into sound; a Song with no instrument sound chosen (an a cappella score) doesn't run it.
- It plays the same `FluidR3_GM.sf2` the browser player loads (`ui/public/soundfonts/`), so the instruments use the same samples; the synth engines differ a little (HLD §9.1).
- Everything else is arithmetic on arrays (numpy, already used) and the loudness meter (`pyloudnorm`, pure Python). `ffmpeg` is already in the API image.
- pyfluidsynth needs the FluidSynth library, which is installed in the `sing-all-package` image (`apt-get install libfluidsynth3`); nobody installs it on a laptop: the trial runs in that image with Docker (M1).

```python
VOCAL_REF_LUFS               = -23
FULL_INSTRUMENTS_DB          = -6     # instruments relative to the vocals
PART_DOMINANT_INSTRUMENTS_DB = -12
TARGET_LUFS                  = -16
PEAK_LIMIT_DB                = -1
MAX_LIMITING_DB              = 3
MAX_VOCAL_GAIN_DB            = 12;  MAX_INSTRUMENT_GAIN_DB = 18

def balanced_mix(vocals, instruments, instruments_db):
    # 1. each voice to VOCAL_REF_LUFS (a silent take is dropped)
    # 2. the instruments as one group, measured only where the voices sing (>= 3 s, else everywhere),
    #    placed instruments_db under the vocal bus
    # 3. the whole mix to TARGET_LUFS; limit peaks to PEAK_LIMIT_DB, never more than MAX_LIMITING_DB of limiting
    return mix, stats            # stats: each stem's loudness and gain; the mix's loudness, peak, limiting

for mode in modes:
    full[mode] = balanced_mix(vocals(mode), instruments, FULL_INSTRUMENTS_DB)          # if mixes in {full, all}
    for part in sung parts of mode:
        dominant[mode][part] = balanced_mix([vocal(part, mode)], instruments, PART_DOMINANT_INSTRUMENTS_DB)   # if mixes == all
                                                                                                                # and an instrument sounds
```

- Instruments: pyfluidsynth renders the MIDI of the job's repeat setting with `FluidR3_GM.sf2`, at the takes' sample rate, after removing the tracks of parts with no sound chosen.
- Everything starts at time 0 with no offset, as in the player.
- Loudness: `pyloudnorm` (new dependency). Limiting and MP3 encoding: `ffmpeg`, already used.

**Mix count and price:** per mode, `full` gives 1 file; `all` gives 1 + (sung parts of that mode). A part with no words has only a solfège take, so it adds a part-dominant file to the solfège mode only.

**No instrument sounds** (every instrument part has no sound chosen, or the score has none): a part-dominant mix would only be that part's solo vocal made louder, the same as its WAV in the zip. So `all` makes the full mixes only, the same as `full` (Alan, 2026-11-19).

| Example | `full` | `all` |
|---|---|---|
| 4 parts, lyrics only | 1 | 5 |
| 4 parts, lyrics and solfège | 2 | 10 |

## The zip

Stored at `singAll/{uid}/{jobId}/{title} - Sing All.zip`; the job records `zipPath`. A title's characters that are unsafe in a file name are replaced.

```text
{title} - Sing All/
  1 Solo vocals/            {part} ({mode}).wav, one per take done
  2 Part-dominant mixes/    {part} ({mode}).mp3            (mixes = all)
  3 Full mixes/             Full mix ({mode}).mp3          (mixes = full or all)
  4 Instrumentals/          {title}.mid                    (when the score has instruments)
  5 Voicebank credits/      {voice}/character.txt, one per voice used
  README.txt                what each folder holds, the parts, voices, mode, verse and repeats; the receipt
```

A partial job zips the takes that were made and the mixes made from them. A job with no take done has no package run.

## API contract (fixed 2026-11-19)

The page is built against these shapes, and the backend tasks build exactly these. A field may be added later; none is renamed or removed without a spec change. Conventions as in step 3's contract (`sing-all-core-step3-spec.md`, "API contract"): sign-in required, camelCase, errors as `{"detail": {"code": str, …}}`. Only what step 4 adds or widens is listed; everything else is unchanged.

### `POST /sing-all/quote`

```text
request   choices.mixes?: "all" | "full" | "none"        # default "all"

200, a quote, fields added:
  options.mixes:    "all" | "full" | "none"               # the choice in effect; "all" is answered as "full"
                                                          # when no instrument sounds
  options.mixFiles: {all: int, full: int}                 # how many mix files each choice makes for the ticked
                                                          # rows and the mode (plan_mixes); "none" is always 0.
                                                          # Customise shows them: "Full and part mixes (10)".
                                                          # No instrument sounds: all == full"
  lines: + {kind: "mixes", files: int, credits: int}      # M6; only when files > 0; placed after "instrumentals",
                                                          # before "total"; "total" includes it
```

| Error | When |
|---|---|
| 422 `invalid_mixes` | `choices.mixes` is not one of the three |

### `POST /sing-all/jobs` ("Sing it")

Request unchanged (`{quoteId}`). The job keeps the quote's `mixes` choice. M6 reserves the mixes with the rest; a `not_enough_credits` answer covers them too.

### `Progress` (`GET /sing-all/active`, `GET /sing-all/jobs/{jobId}`, "Sing it")

```text
fields added or widened:
  stage:   "reading" | "splitting" | "solfege" | "singing" | "packaging" | "done"
  choices: {…step 3's fields, mixes: "all" | "full" | "none"}      # a job started before step 4 reads "none"
  package: null | {state: "mixing" | "zipping" | "done" | "failed",
                   mixes: {done: int, total: int}}                  # total 0 with mixes "none"
           # null before the packaging stage, and for a job that ended with no take done.
           # "done": the zip exists and can be downloaded. "failed": no zip, the mixes are refunded,
           # and status still follows the takes ("completed" or "partial")
  receipt[i].type:  … | "mixes"                                     # M6
  receipt[i].files: int | null                                      # M6; the number of mix files on that row, else null
           # mixes give at most two rows: the files made ("charged") and the files not made ("refunded")
```

The card's stages: "Mixing (done of total)" while `package.state` is `mixing`, then "Packing the zip" while it is `zipping`. With mixes `none`, only "Packing the zip".

### `GET /sing-all/jobs/{jobId}/download` (new)

```text
200 {url: str, fileName: str, expiresAt: str}
    # url: opens the zip directly, with no sign-in header (the page navigates to it).
    #      In production a signed storage URL, valid 15 minutes, whose response has
    #      Content-Disposition: attachment; filename="{fileName}". With the storage emulator,
    #      a URL the emulator serves the same bytes from.
    # fileName: "{title} - Sing All.zip", the zip's own name (unsafe characters replaced)
    # expiresAt: ISO 8601
```

| Error | When |
|---|---|
| 404 `job_not_found` | No such job, not the caller's, or its Song is deleted (the same answer for all three) |
| 409 `zip_unavailable` | `package.state` is not `done` |

### `POST /sessions/{id}/stop`

Shape unchanged. While the job's stage is `packaging`, `singAll.result` is `too_late` and the run finishes.

### `GET /songs/{id}/workspace`

Shape unchanged. After a job's package run is done, `player` holds the opening balance (M5), so the page needs no change to show it.

### Who builds which field

| Part | Task |
|---|---|
| `choices.mixes`, `options.mixes`, `options.mixFiles`, `invalid_mixes`; `stage: "packaging"`, `choices.mixes` and `package` in Progress; the download endpoint; Stop too late; the opening balance | M5 (Codex) |
| The `mixes` quote line and the total; the reservation; the receipt's `mixes` rows and `files` | M6 (Claude) |

Until M6 merges, the mixes are made at no charge and the quote has no `mixes` line. That state stays on the parent branch.

## Who builds what

| Task | Who | Needs |
|---|---|---|
| **M1. The mixing code, the trial command, the image and the sync check** | Codex | — |
| **M2. The listening trial** | Claude runs it, Alan listens | M1 |
| **M4. The package run** | Codex | M1 |
| **M5. Backend: the mixes choice, the packaging stage, Progress, the download, the opening balance** | Codex | M4 |
| **M6. Credits for the mixes** | Claude | M5 |
| **M7. UI** | Claude | the contract above (mocked); M6 for the real-backend run |
| **M8. Deployment** | Alan | M6, M7 |

Codex builds the backend; Claude builds the UI and the credit code (billing is off limits for Codex, protocol §8). The numbers M2 settles go into M1's constants before M5 ships. M3 ("Sing All takes keep a WAV") was dropped: every take already keeps one (difference 5). The task numbers are kept.

Every Codex task:
- **Parent:** branch `feature/sing-all-mixes`, cut from `feature/share` (it holds Sing All core, its UI and Share). PR into it, never main. Design: HLD §6, §9, §9.1, §16 row 4, on branch `docs/sing-all-process-hld`; where this spec and the HLD differ, this spec wins.
- **Scope:** `src/backend/`, `scripts/`, `tests/`, `requirements*.txt`, the messages file and `Dockerfile.sing-all-package`. Not `credits.py` or any billing module, not `firestore.rules` or `storage.rules`, not the UI, not deploy scripts or prod env.
- **Checks (paste baseline vs after outputs in the PR):** the step 3 command as the share spec gives it (`share-step7-codex-spec.md`, S1 "Checks"), with `tests/test_songs_backend.py`, `tests/test_sing_all_*.py` and the task's new test files added. Measure the baseline on the parent commit first; known failures stay the same, with no new ones. `git diff --check` is clean.

## M1. [Codex] The mixing code, the trial command, the image and the sync check

### Rule

The mix rule above, as pure functions over audio arrays, plus the instrument render, plus a command that mixes one Song's files on a laptop.

### Change

1. `src/backend/sing_all_mix.py`: `lufs`, `balanced_mix` (returns the mix and its stats), `limit` (ffmpeg's limiter, replacing hard clipping), `render_instruments(midi_path, soundfont_path, sample_rate, drop_tracks) -> numpy array` (pyfluidsynth, driven by `mido`, as above), `plan_mixes(takes, modes, mixes)` (which files, with their names), and `write_mp3`. Reading audio reuses `audio_mix._read_audio`.
2. `scripts/sing_all_mix_trial.py`: given a folder of take WAVs (or MP3s), a MIDI file and the part names, writes the full and part-dominant mixes and prints each stem's loudness and gain and each mix's loudness, peak and limiting. Constants can be overridden on the command line (`--full-instruments-db -6` …) so Alan can try values without editing code.
3. `requirements*.txt`: `pyloudnorm`, `pyfluidsynth`, `mido`. `pyfluidsynth` is imported inside `render_instruments` only, so the API and tests run without the FluidSynth library.
4. `Dockerfile.sing-all-package`: Python slim, `ffmpeg`, `libfluidsynth3`, the backend requirements and the code; the soundfont is mounted or copied from `ui/public/soundfonts/FluidR3_GM.sf2` (not in git). The trial runs in it: `scripts/sing_all_mix_trial.sh <folder>` builds the image once and runs the trial command with the folder mounted, so Alan's Mac needs only Docker. M8 deploys the same image.
5. **The sync check, for testing only** (Alan, 2026-11-19): `scripts/sing_all_sync_check.py`, never imported by `src/` and never run by a job. The instrument MIDI holds no vocal part (`performance_midi.py` leaves out parts with lyrics), so the check places the sung part's notes on the MIDI's own clock instead of rendering them:

   ```python
   def measure_offsets(take_wav, midi_path, score, part, repeats):
       clock  = the MIDI's tempo map, read with mido (tick -> seconds)
       sung   = the score the take was sung from: expanded_score_for_synthesis(score) if repeats, else score
       # 1. the take against the MIDI's clock
       expected = [clock(beat) for each non-rest note start of `part` in `sung`]
       heard    = note starts in the take: rises in frame energy (numpy; no new dependency)
       vocal    = median(nearest heard - expected), pairing within 150 ms; and how many paired
       # 2. the MIDI's notes against the same clock (catches a repeat order that differs)
       expected = [clock(beat) for each note start of the instrument parts in `sung`]
       written  = the MIDI's note_on times
       instruments = median(nearest written - expected), pairing within 50 ms; and how many paired
       return {vocal_ms, vocal_paired, instruments_ms, instruments_paired}
   ```

   It needs no FluidSynth.
   The trial command prints it per take; the tests below use it. A mix never calls it.

### Done when

- Unit tests on synthetic signals (sine tones, silence, a loud intro then quiet verses):
  - two vocals at different levels come out within 0.5 LU of each other;
  - instruments measured only where the voices sing: a loud intro alone does not lower them under the verses;
  - a silent take is dropped, and a near-silent stem is not raised by more than its cap;
  - every mix ends within 0.5 LU of `TARGET_LUFS`, unless limiting would pass `MAX_LIMITING_DB`, in which case it is quieter and limited by at most that;
  - no sample is above `PEAK_LIMIT_DB` after limiting;
  - `plan_mixes` gives the counts of the table above.
  - with no instrument sounding, `plan_mixes` gives the same files for `all` as for `full`.
- `render_instruments` has one test that runs only when the FluidSynth library loads (`pytest.mark.skipif`), and checks the array's sample rate, length, that a dropped track is silent, and that the result is not silence.
- Sync (the sync check): for test fixtures with repeats (both settings), a pickup bar and a tempo change, `vocal_ms` and `instruments_ms` are within 30 ms, with at least 80% of the notes paired. The takes are sung at test time with the local voicebank through `src/api/synthesize.synthesize`, and the test is skipped when no voicebank is installed. No sung WAV is committed (the voices' terms of use). A failure names the score feature and is reported to Claude as a `[Codex] Question:`, not fixed in the mix code: the take and the MIDI must agree at the source.
- The sync check's own logic has unit tests without a voicebank: a synthetic click track built from a score's beats on a MIDI's clock measures 0 ms, and the same track shifted by 40 ms measures 40 ms.
- The trial command runs on the repo's test fixtures and prints the stats, both directly and through `scripts/sing_all_mix_trial.sh` (Docker), where the pyfluidsynth test also runs.

### Checks

As under "Who builds what", with `tests/test_sing_all_mix.py` added.

### Out of scope

The job, the zip, storage, credits, the UI.

## M2. The listening trial (Claude runs it, Alan listens)

First song: the in-app demo **Amazing Grace** (Alan, 2026-11-19), the score a user can load from "Demo songs" when they have no MusicXML: `ui/public/demo-scores/amazing-grace.xml`, SATB choir and piano, 2 verses. (Not the landing page's a cappella copy in `ui/public/landing/demo/`.)

1. **Takes:** Claude makes the four parts' lyrics takes, verse 1, with the local backend (the same synthesis the app runs), as WAV. Nothing is downloaded from production.
2. **MIDI:** the Song's performance MIDI from the local backend, with the piano's default sound.
3. **Run:** the trial command on those, which writes the lyrics full mix and the four part-dominant mixes and prints each stem's loudness and gain, each mix's loudness, peak and limiting, and each take's measured offset from the MIDI (the sync check), so the sync is a number, not only a listen.
4. **Compare:** the full mix next to a recording of the browser player playing the same Song at its default balance, so pyfluidsynth's piano can be judged against the browser's.

Claude runs it through `scripts/sing_all_mix_trial.sh` (Docker, nothing installed on the Mac) and sends Alan the mixes and the stats; Alan listens and sets `FULL_INSTRUMENTS_DB` and `PART_DOMINANT_INSTRUMENTS_DB`, with the command's overrides for a second try if needed. Later songs (one with a loud intro, one a cappella) only if Amazing Grace leaves a question open.

## M4. [Codex] The package run

### Rule

Given a Sing All job whose takes have ended, make its mix files and its zip, store them, and report what was made. It doesn't touch credits or the job's status: the reconciler (M5) does.

### Change

1. `src/backend/sing_all_package.py`: `run_package(job_id)`:
   - reads the job, its done take items (each child job's `losslessOutputPath`, else its `outputPath` when that is a WAV), the Song's score summary, MIDI and voices;
   - writes progress on the job in the contract's shape: `package.state` `"mixing"` then `"zipping"`, and `package.mixes.done` after each mix file;
   - makes the mixes by `plan_mixes` and `balanced_mix`, encodes them to MP3;
   - builds the zip as above and uploads it, and the mixes beside it under `singAll/{uid}/{jobId}/`;
   - writes `package.state = "done"` with the internal fields `zipPath`, `files: [{name, kind, mode, partId}]` and `stats` (each stem's gain and loudness, as `balanced_mix` returns them), or `package.state = "failed"` with `error`; then enqueues reconcile. `package.updatedAt` is written with every change (the sweeper's 10-minute limit reads it). Progress (M5) exposes only `state` and `mixes`.
   - Idempotent: a second run for the same job overwrites the same paths and writes the same result.
2. Entry points: `python -m src.backend.sing_all_package <jobId>` (the Cloud Run Job), and an inline call for local runs and tests, chosen by a setting `SING_ALL_PACKAGE_TRANSPORT = inline | job`, as the render transport is.
3. The soundfont path is a setting (`SING_ALL_SOUNDFONT_PATH`); in the image it points at the copied soundfont.

### Done when

- On a job with two done takes and instruments, the run writes the zip with the layout above, the mixes and the README, and `package.state = "done"`; the README lists the parts, voices, mode, verse, repeats and receipt.
- A partial job zips only the takes made.
- A missing WAV, an instrument render failure or upload failure gives `package.state = "failed"` with the error, and nothing half-written is recorded as done.
- A second run gives the same files and result.
- Tests use the inline transport and a fake `render_instruments` (a fixed array) unless the FluidSynth library loads.

### Out of scope

Credits, the job's stages and status, the UI, the Cloud Run Job's deployment.

## M5. [Codex] Backend: the mixes choice, the packaging stage, Progress, the download, the opening balance

### Rule

```python
# quote: choices.mixes (default "all"; anything else 422 invalid_mixes)
#   options.mixes = the choice, "full" in place of "all" when no instrument sounds; options.mixFiles = {all: plan_mixes(rows ticked, mode, "all") count,
#                                                   full: plan_mixes(…, "full") count}
#   no "mixes" line and no price: M6 adds them
# "Sing it": job.choices.mixes = the quote's choice

# reconciler, when the singing stage ends:
if no take done:
    finish as today ("failed"); package stays null
else:
    job.stage = "packaging"; job.package = {state: "mixing", mixes: {done: 0, total: planned files}}
    start the package run (M4) by SING_ALL_PACKAGE_TRANSPORT
    # the run writes package.state "mixing" -> "zipping" and mixes.done as it goes
    on package.state == "done":
        set the player's opening balance (below)
        finish("completed" if every take done else "partial")
    on package.state == "failed", or no package progress for 10 minutes (the sweeper):
        package.state = "failed"
        finish by the takes, as above            # the status is never "failed" because of the package

# Stop while stage == "packaging": singAll.result "too_late"; the run finishes
# a job started before step 4 (no choices.mixes): mixes "none"; it still gets its zip
```

The player's opening balance, from HLD §9.1:

```python
mode  = "lyrics" if the job sang lyrics else "solfege"     # the mode the player shows first
gains = the full mix's step 1 gain of each vocal of that mode, and step 2's instrument gain (package.stats)
shift = max(gains)                                         # the player's volume tops out at 1
vocal.volume      = db_to_gain(vocal.gain - shift)         # the job's own tracks of that mode
instrument.volume = 1;  bus.level_db = instruments.gain - shift
# Only values still equal to what Sing All wrote when it added the track are replaced:
# a volume, mute, solo or bus level the owner changed during the job stays as they set it.
# With mixes "none" the stats are still measured, so the balance is set the same way.
```

The download: `GET /sing-all/jobs/{jobId}/download`, exactly as the contract says. The signed URL is made with the API's own service account (IAM `signBlob`; M8 grants it), with `response_disposition` set to the attachment header. With the storage emulator, the URL is one the emulator serves without a signature.

### Done when

Each new test fails on the parent commit.
- **Quote:** `options.mixes` defaults to `all` and follows `choices.mixes`; `options.mixFiles` matches the table under "The mixing rule" (4 parts lyrics only: all 5, full 1; both modes: all 10, full 2) and follows ticked rows; `invalid_mixes` for any other value; the lines and total are unchanged.
- **Stage:** after the last take, a job with takes done goes to `packaging` with `package.state = "mixing"` and the planned total; the run's progress shows in `GET /sing-all/jobs/{id}`; on done the job ends `completed` or `partial` by its takes, and `package.state = "done"`.
- **No take done:** the job ends `failed` as in step 3 and `package` is `null`.
- **Package failure:** a failing run, and a run with no progress for 10 minutes (sweeper, with a fake clock), give `package.state = "failed"` and a job status from its takes.
- **Stop:** during `packaging` the stop answers `too_late`, the run finishes, and the job ends by its takes.
- **Opening balance:** after a job, the workspace player's volumes and bus level follow the stats as above; a volume the owner changed during the job, and a mute, stay.
- **Download:** the owner gets `{url, fileName, expiresAt}` and fetching `url` gives the zip's bytes; another user, a missing job and a job whose Song is deleted get the same 404 `job_not_found`; a job whose package is not done gets 409 `zip_unavailable`.
- **Old jobs:** a job document with no `choices.mixes` reads `mixes: "none"` in Progress.
- No job id, session id, user id or storage path appears in a log line at INFO or above.

### Out of scope

Credits: no reservation, settlement, quote line or receipt row for the mixes (M6). The UI (M7). Emails (step 5).

## M6. [Claude] Credits for the mixes

```python
MIX_CREDITS_PER_FILE = 1
# quote: lines += {kind: "mixes", files: n, credits: n * MIX_CREDITS_PER_FILE} when n > 0, where n = mixFiles[choice]
#        total includes it; blocked "not_enough_credits" counts it
# "Sing it": one more reservation, "{jobId}-mixes", in the same transaction as the others
# at the end:
#   package done   -> settle len(mix files made) * MIX_CREDITS_PER_FILE; refund the rest
#   package failed, or no take done -> refund all of it
# receipt: one "mixes" row for the files made (charged) and one for the files not made (refunded), each with files
```

Done when: the quote prices the mixes by choice; the reservation, settlement and refunds add up to the quote total in every ending (completed, partial, package failed, no take done, stopped before packaging); the existing credit tests pass unchanged.

## M7. [Claude] UI

- Customise: "Mixes" as a three-way choice, "Full and part mixes ({mixFiles.all})", "Full mix only ({mixFiles.full})", "None"; when no instrument sounds (`mixFiles.all == mixFiles.full`), two: "Full mix ({mixFiles.full})" (sends `full`) and "None". It re-quotes like the other choices. The quote table gets the "Mixes · {files} files" line.
- Progress: the stages above ("Mixing (d of t)", "Packing the zip").
- The ended card: **Download zip** when `package.state == "done"` (calls the download endpoint, then navigates to `url`; a 409 or 404 shows "The zip isn't available."); "The mixes couldn't be made. Your parts are in the player." when `package.state == "failed"`; the receipt's mixes rows ("Mixes (N files)", charged or refunded).
- Playwright: mocked card states (choice, stages, done, failed, download), and one real-backend run that downloads the zip and checks its folder names.

## M8. Deployment (Alan)

The `sing-all-package` Cloud Run Job (CPU, 4 vCPU / 8 GiB, us-east4) from `Dockerfile.sing-all-package` (the FluidSynth library and the FluidR3 soundfont); `SING_ALL_PACKAGE_TRANSPORT=job` and the job's name in the API's env; its service account can read and write the bucket and the job documents. The sweeper's packaging limit needs no new trigger. The API's service account may sign URLs for itself (`roles/iam.serviceAccountTokenCreator` on itself), for the download.

## Decisions (Alan, 2026-11-19)

1. **Mixes:** `all` by default (the full mix plus one part-dominant mix per sung part), with "Full mix only" and "None" under Customise; 1 credit per file.
2. **A failed package run:** no automatic retry. The job ends by its takes, the mixes are refunded, no zip.
3. **One `packaging` stage**, and **Stop during packaging is too late**: agreed.
4. **The trial:** Amazing Grace first. The mix is an offline render in Python with pyfluidsynth for the instruments (in process, no command line), never a browser; the FluidSynth library is in the package image, so nothing is installed on the Mac.
5. **Deleting:** logical only. Nothing is removed (difference 8).
6. **No instrument sounds:** only the full mixes; Customise offers "Full mix" and "None".

Follow-on from 5: **Download zip** lives only on the Song's job card, so a deleted Song (which can't be opened) has no way to it. The download endpoint answers 404 for a job whose Song is deleted, the same as the Song's own endpoints, so a saved request can't reach it either. The files stay in storage.
