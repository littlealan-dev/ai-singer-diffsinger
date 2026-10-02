# Demo Songs, Birthday Name Replacement, and Score-Edit Staleness LLD

**Status:** Agreed design, not yet implemented
**Scope:** Let users try the app with two built-in demo songs; let the Happy
Birthday demo sing a user-supplied name in place of its placeholder; and make
every artifact derived from a score's music (takes, generated solfege lines,
instrumental MIDI) react correctly when a tool edits that music.

## 1. Decisions

| Topic | Decision |
|---|---|
| Demo songs | Two: **Amazing Grace** (SATB + piano, 2 verses) and **Happy Birthday** (alto + men + piano, men written an octave lower). |
| Demo delivery | Static MusicXML in `ui/public/demo-scores/`, loaded through the normal upload request. No backend endpoint. |
| Where demos appear | Empty score panel (cards), empty chat (links), and the composer's "+" menu. |
| Starter prompts | Per demo song while that song is the current score; generic prompts otherwise. |
| Name replacement scope | Only the Happy Birthday demo. Positions are hard-coded; no lyric search. |
| Name replacement tool | `replace_birthday_name`, deterministic. Offered to the LLM only when the current score is the Happy Birthday demo. |
| Name validation | Run the synthesis's own phonemizer; reject what it rejects; the vowel count is the syllable count. No character rules. |
| Rhythm | All syllables except the last on beat 1, the last on beat 2 (table in §5.4). 5 or more syllables are rejected. |
| Flow | Like Add Solfege: edit the score, refresh the preview, and in the same round ask the user to review it and present the quote. |
| Default part to quote | Alto (it carries the melody). |
| Score-edit staleness | Per-part musical signature, computed by the parser. Derived artifacts record the signature they were built from. |
| Stale takes | Replaced when the next take lands; the quote warns which takes will be replaced. |
| Stale solfege lines | Regenerated in the same step as the edit. |
| Instrumental MIDI key | Instrumental part signatures plus score-wide timing, instead of the whole MusicXML file. |
| Conditional tool spec | Sent in the per-request (dynamic) prompt context, so the cached static prompt stays identical for every score. |
| Demo flag on jobs | Every job records `demoSongId` (null for a user's score), taken from a marker inside the demo MusicXML, never from the title or file name. |

## 2. Demo songs

### 2.1 Files

| Song | Shipped file | Source |
|---|---|---|
| Amazing Grace | `ui/public/demo-scores/amazing-grace.xml` | `assets/test_data/amazing-grace-demo.xml` (title fixed, verse 3 removed, verse 2 copied to all voices, repeats removed) |
| Happy Birthday | `ui/public/demo-scores/happy-birthday.xml` | `assets/test_data/happy-birthday-alto-men-piano-8vb.xml` (soprano removed, heading "Happy Birthday", "you ___" placeholder with slur, men in treble-8vb sounding an octave lower) |

Both files get a **demo marker** in `<identification>`, with the song's id:

```xml
<miscellaneous>
  <miscellaneous-field name="sightsinger-demo">amazing-grace</miscellaneous-field>   <!-- or happy-birthday -->
</miscellaneous>
```

The Happy Birthday file also gets **divisions 12** in the Alto and Men parts
(currently 4). Every `<duration>`, `<backup>`, and `<forward>` in those parts is
multiplied by 3. A triplet eighth is then 4 divisions; at 4 per quarter it
would be 4/3.

`ui/public` is tracked by git (`assets/` is not), so the shipped copies are
committed. Rights: both arrangements must be cleared for redistribution before
release.

### 2.2 UI

```ts
DEMO_SONGS = [
  { id: "amazing-grace", title: "Amazing Grace", detail: "SATB choir + piano · 2 verses",
    file: "/demo-scores/amazing-grace.xml",
    prompts: ["sing the soprano part, verse 1", "sing the alto part in solfege", "sing the bass part, verse 2"] },
  { id: "happy-birthday", title: "Happy Birthday", detail: "Alto & men + piano",
    file: "/demo-scores/happy-birthday.xml",
    prompts: ["sing the alto part", "sing the men's part", "sing the alto part in solfege"] },
]

loadDemoSong(song):
    creditsLocked → paywall ("upload_blocked"), like an upload
    log analytics "demo_song_load" {demo_song_id}
    file = new File([fetch(song.file)], `${song.id}.xml`)
    handleUpload(file, song.id)          # same path as a user's file
    on success: activeDemoSongId = song.id → starter prompts = song.prompts
a user's own upload → activeDemoSongId = null → generic prompts
```

The "+" control in the composer toolbar opens a menu: "Upload MusicXML…", then
a "Demo songs" section listing both songs. The hidden file input keeps
`data-testid="score-upload-input"`.

Already implemented and tested (uncommitted) with the previous Happy Birthday
file; step 1 of §9 swaps in the new file, detail, and prompts.

## 3. Demo detection in the parser

```
DEMO_SONG_IDS = {"amazing-grace", "happy-birthday"}

parse_score:
    score_summary.demo_song =
        the marker's value   if the raw MusicXML has
                             <miscellaneous-field name="sightsinger-demo">…</…>
                             and the value is in DEMO_SONG_IDS
        absent               otherwise (unknown values are ignored)
```

The marker is in `<identification>`, so every derived score written from a
demo (name edits, solfege lines, part splitting) keeps it. Jobs rendered from an
edited demo still record their `demoSongId` (§3.1), and the Happy Birthday tool
stays available to change the name again or restore the original.

A user who downloads a demo file and uploads it, or copies the marker into
their own file, gets the same flag. The flag means "this score carries a demo
marker", which is what analytics needs; the name tool's own placeholder check
(§5.2) still refuses any score whose measure 7 differs.

### 3.1 Job record

The job's input provenance (`_capture_job_input` in the orchestrator, written
once at job creation and immutable in `job_store.update_job`) gains one field
next to `scoreTitle` and `inputFileName`:

| Field | Value |
|---|---|
| `demoSongId` | `score_summary.demo_song` (`"amazing-grace"`, `"happy-birthday"`), or `null` for a user's score |

A non-null value is the demo flag. The field is added to the immutable
provenance set, so a later update cannot change it. Jobs created before this
change have no field and count as non-demo.

## 4. Conditional tool availability

Today the tool list is plain JSON text inside the static system prompt
(`{tool_json}`, `system_prompt.txt`), which is what the Gemini prompt cache
keys on. Allowed calls are a fixed set per role (`DEFAULT_LLM_TOOL_ALLOWLIST`,
checked by `_first_invalid_tool_for_role`).

```
CONDITIONAL_TOOLS = {
    "replace_birthday_name": lambda score_summary: score_summary.get("demo_song") == "happy-birthday",
}

for every LLM call:
    available = role allowlist ∪ {tool for tool, condition in CONDITIONAL_TOOLS if condition(current summary)}
    static prompt  → role tools only (unchanged, so one cached static prompt for every score)
    dynamic context → "Tools available for this score:" + JSON specs of the conditional tools that apply
    execution       → any call outside `available` takes the existing invalid-tool path
```

The executor's own set of runnable tools (`_execute_tool_calls`) also lists
`replace_birthday_name`; availability is decided by the check above.

All instructions for `replace_birthday_name` live in its own description (when
to call it, the `sung_text` format, the edit → review + quote flow). The general
system prompt gets no lines about it, so a score without the tool shows the LLM
nothing about names.

The tool also checks the marker and the expected notes itself (§5.2), as a
second line of defence.

## 5. `replace_birthday_name`

### 5.1 Interface

```
replace_birthday_name(
    name: string | null,     # null restores the original "you ___"
    sung_text: string,       # the name split into syllables: "Hen-ry", "An-na Ma-rie"; omitted when name is null
    voicebank: string,       # resolved like the quote: the UI selection overrides
)
→ success (tool):      { status: "name_ready", unchanged, sung_text, derived_score, score_summary, derived_musicxml_path }
→ success (LLM sees):  { status: "name_ready", sung_text, score_changed, message }
→ action_required: name_not_singable | name_syllables_mismatch | name_too_long | demo_placeholder_not_found
```

`unchanged: true` when the placeholder already holds exactly this name and
rhythm (compared on pitch, duration, type and verse-1 lyric): nothing is written,
no new score version is saved, and `score_changed` is false.

The LLM interprets the request ("sing it for Henry") and supplies `sung_text`.
Everything else is deterministic.

### 5.2 Fixed positions (Happy Birthday demo only)

| Part | Measure | Beat 1 (original) | Beat 2 (original) |
|---|---|---|---|
| Alto (`P2`) | 7 | E4 quarter, lyric "you" + extend, slur start | D4 quarter, slur stop |
| Men (`P3`) | 7 | C3 quarter (written C4, treble-8vb), lyric "you" + extend, slur start | D3 quarter, slur stop |

Before editing, the tool checks the demo marker and that measure 7 begins with
notes summing to two quarters whose first pitch and last pitch match the table
(the original pair, or a pair it split earlier). Otherwise it returns
`demo_placeholder_not_found` and changes nothing.

Beats 1–2 of measure 7 are always **rebuilt from the original pitches in the
table**, so a second name, or `null`, replaces any earlier split cleanly.

### 5.3 Validation

```
name is null → no validation; restore "you"
sung_text empty, or an empty piece ("Hen--ry")                         → name_syllables_mismatch
sung_text without hyphens or spaces != name without spaces (case-insensitive) → name_syllables_mismatch
for each word in sung_text.split(" "):
    phonemes = phonemize([word without hyphens], voicebank, language="en")   # the synthesis's own call
        UnsupportedLyricTokenError → name_not_singable {word, reason, message from the phonemizer}
    vowels = count(p for p in phonemes if phonemizer.is_vowel(p))
        vowels == 0 → name_not_singable {word}
total = sum(vowels)
total > 4 → name_too_long {syllables: total, max_syllables: 4}      # checked before the split,
                                                                      # so a long name is never asked to re-split
for each word: vowels != len(word.split("-")) → name_syllables_mismatch {word, expected_syllables: vowels}
```

Measured on both installed voicebanks (LIEE, Qixuan), with identical counts:

| Names | Result | Syllables |
|---|---|---|
| Tom, Jack, Pete | ok | 1 |
| Henry, Alan, Siobhan, Nguyen, O'Neil, Zoë | ok | 2 |
| Jonathan, Mary-Jane | ok | 3 |
| Alexander, Anastasia, Anna Marie | ok | 4 |
| 李明 | rejected: non-Latin text the English fallback can't phonemize | |
| 123 | rejected: only numbers or punctuation | |

### 5.4 Rhythm

| Syllables | Beat 1 (same pitch as the original beat 1) | Beat 2 (original beat-2 pitch) | Example |
|---|---|---|---|
| 1 | quarter: the whole name, extend, slur start | quarter: slur stop | "Tom ___", "Jack ___" |
| 2 | quarter: syllable 1 | quarter: syllable 2 | "A-lan", "Hen-ry" |
| 3 | two eighths: syllables 1, 2 | quarter: syllable 3 | "Jo-na \| than" |
| 4 | triplet eighths: syllables 1–3 | quarter: syllable 4 | "A-le-xan \| der" |

`<syllabic>` follows word structure: `single`, or `begin` / `middle` / `end`
across a word's pieces. For "Anna Marie": An(begin) na(end) Ma(begin) | rie(end).
Triplets carry `<time-modification>3:2</time-modification>` and `<tuplet>`
start/stop notations.

Why a 1-syllable name needs no extra vowel note: the English slur distribution
(`phoneme_logic_handler_en.py`) gives note 1 the onset and vowel and the last
note the vowel and coda, so "Jack ___" sings JH AE | AE K. A spelling like
"Ja-ak" would make note 2 silent, because "Jack" has one vowel.

### 5.5 Other lyric lines on split notes

A generated solfege line on Alto or Men is not copied onto split notes; §6.3
regenerates it from the edited notes.

### 5.6 Flow

```
user: "sing it for Henry"
LLM  → replace_birthday_name(name="Henry", sung_text="Hen-ry", voicebank=…)
tool → writes score-name-<uuid>.xml, saves it as the active score (new version, refreshed summary)
     → §6.3 regenerates stale solfege lines
     → follow-up (tools allowed): {status: "name_ready", sung_text: "Hen-ry", score_changed, message}
LLM  → prepare_synthesis_quote(part Alto unless the user named one, lyric line 1, …)
     → message-only: "I've written Hen-ry into measure 7 — check the preview" + quote table + confirmation request
UI   → the reply carries current_score → refreshScorePreview()
user: "yes" → synthesize (normal billable confirmation rules)
```

Persisting follows `_persist_solfege_result`: the MCP tool returns the derived
score, its summary, and the derived MusicXML path, and the orchestrator saves
it with `set_score(..., baseline=True)`.

## 6. Score-edit staleness

### 6.1 Signatures (parser)

```
timing_signature (score-wide) =
    hash(time signatures, tempo marks, measure durations,
         repeat barlines, endings, D.C./D.S./Coda/Fine)

part_signature(part) =
    hash(note events in order: offset, pitch, duration, tie, rest, voice;
         authored lyric lines: number, text, syllabic, extend)
    generated solfege lines (name "SightSinger Solfege") are excluded: they are derived

score_summary.timing_signature = timing_signature
score_summary.parts[i].part_signature = part_signature(part i)
score_summary.parts[i].take_signature = hash(timing_signature, part_signature(part i))
```

Not changed by: switching lyric line or verse, adding or regenerating a solfege
line, selecting a part, or adding derived parts with part splitting (the
original parts' signatures stay the same).

### 6.2 Takes

```
synthesis job payload: take_signature = summary.parts[sung part].take_signature   # at render time
completed job → UI take record carries take_signature
UI chat body score_player_takes: [{part_id, label, expand_repeats, take_signature}]

quote → takes_removed_from_player.takes = [
    {label, reason: "rendered_with_repeats" | "rendered_in_written_order"}  for takes in the other repeat order (existing rule)
    {label, reason: "score_edited"}    for takes whose take_signature != current take_signature of their part
]

new take lands → the UI removes every take with the other repeat order (existing)
                 and every take whose take_signature != its part's current take_signature
```

`takes_removed_from_player` changes from `{parts, rendered_with_repeats}` to a
list with a reason per take. The prompt's quote rule is updated to name each
take and its reason: rendered in the other repeat order, or sung before the
score was edited. Takes stay available in the chat.

### 6.3 Generated solfege lines

```
after any tool round that saves a new score:
    changed = parts whose part_signature differs from before the round
    for part in changed with a generated solfege line:
        remove the line; add_solfege_lyric_verse(part, current solfege settings)
    save as a further version (same round)
```

Solfege generation is deterministic, so regeneration needs no stored record.

### 6.4 Instrumental MIDI

`instrumental_midi_source_signature` changes from hashing the whole MusicXML
file to:

```
hash(PERFORMANCE_MIDI_VERSION, score_id, timing_signature,
     part_signature of every instrumental MIDI part, playback presets)
```

Vocal-only edits, lyric lines, and solfege lines no longer rebuild the MIDI.
Timing is score-wide because the MIDI takes its tempo map from the whole score
(`_canonical_tempo_events`) and expands repeats.

The one-time instrumental charge stays scoped to `uid/session/score_id`, so a
rebuilt MIDI is never charged again.

## 7. Prompt and tool text

- `replace_birthday_name` description: when to call it (the user asks to sing
  for someone, on this demo), the `sung_text` format, null to restore "you",
  the rejection codes and what to tell the user for each, and that after
  `name_ready` it must call `prepare_synthesis_quote` (Alto by default) and
  include the review request in the quote message.
- Quote rule for `takes_removed_from_player`: one sentence naming each take and
  its reason.
- No other system-prompt changes.

## 8. Tests

| Area | Tests |
|---|---|
| Demo UI (e2e, mocked) | cards and links; each demo uploads its own file; per-song prompts; the "+" menu; a user upload restores the generic prompts |
| Demo detection | each demo's marker sets `demo_song`; unknown values and unmarked scores don't; a derived score (name edit, solfege) keeps it |
| Job flag | a synthesis job from each demo records its `demoSongId`; a job from a user's score records `null`; `update_job` rejects the field after creation |
| Conditional tool | listed in the dynamic context only for the demo; a call on another score takes the invalid-tool path; the static prompt is identical with and without the demo |
| Validation | each row of §5.3; mismatched `sung_text`; 5 syllables rejected |
| Rhythm | 1–4 syllables produce the notes of §5.4 in both parts, with correct durations (divisions 12), syllabic, extend, slur, and tuplet; `null` restores the original; renaming after a split rebuilds from the original |
| Placeholder check | a modified measure 7 returns `demo_placeholder_not_found` without changes |
| Signatures | unchanged by lyric-line switch, solfege, part splitting; changed by a note or authored-lyric edit; timing changes alter every take signature |
| Stale takes | the quote lists `score_edited` takes; the UI removes them when the next take lands (e2e) |
| Solfege regeneration | a name edit regenerates an existing Alto solfege line on the split notes |
| MIDI key | a vocal-only edit keeps the MIDI; an instrumental note edit or a tempo change rebuilds it |
| Chat flow (stub LLM) | name → `name_ready` → quote in the same turn; the reply carries `current_score` |

Real-LLM tests remain disabled; chat flows use stub LLMs.

## 9. Build order

Each step is tested and committed separately.

1. **Demo songs:** add the marker to both files and divisions 12 to Happy
   Birthday; ship them; update the Happy Birthday card detail and prompts;
   parse the marker into `score_summary.demo_song` and record `demoSongId` on
   jobs; commit the demo feature.
2. **Signatures** in the parser, and the narrowed instrumental MIDI key.
3. **Stale takes:** `take_signature` on jobs and player takes; the quote's
   reasons; the UI removal; the prompt rule.
4. **Solfege regeneration** after a score change.
5. **`replace_birthday_name`:** demo detection, conditional availability, the
   MCP tool, the orchestrator flow, and the tool description.
