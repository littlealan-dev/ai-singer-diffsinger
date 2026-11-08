# Borrowed words: read the donor staff's lyric line, not a voice

Status: building 2026-11-08. Linear: SIG-182 (§4c), SIG-183 (§4a, §4b, §5, §6), both Codex tasks.
Area: planner v2 (sheet, assembler, prompt) and the shared executor (`src/api/voice_parts.py`).
v1 never writes the new plan field, so v1 behaves as today.

## 1. The situation

A lane whose staff has no words (a lower staff with no words under an upper staff that carries the text)
borrows its words from another staff. Today the sheet offers each donor as a **voice** of another
part, meaning one of the parser's voice parts, and asks the question once per group of bars.

The parser's voice label is not stable from bar to bar. music21 keeps a written voice number only
in bars that use two or more voices. In a bar where every note says `<voice>1</voice>`, it drops the
number, and the parser files the notes under the staff's **shared line**.

| Bar | Written in the MusicXML | Parser's label |
|---|---|---|
| 8 | voice 1 (chords) + one voice-2 note | "1" and "2" |
| 9 | voice 1 only (chords) | shared line |

## 2. What goes wrong

In the failing score the verse is a single row of words: upper staff, lyric line 1, every word typed
on voice 1.
The sheet offers it as two different sources: "voice 1" (bars with a second voice) and "shared line"
(bars without one). A group of bars that has both kinds has no right answer. Either choice leaves
bars without words, the after-split check fails, and only the repair's narrower groups fix it.

**Evidence.**
- A local run (a two-staff choir score; upper staff split into 2, lower into 3): on the first attempt
  the lower lanes' groups 8–9 and 11–13 failed the lyric check in bars 9 and 13. The repair asked bars 9 and 13 on their own and passed.
- Corpus, 40 jobs, sheets built offline: 88 borrowed-word questions in 7 jobs. In 67 of them
  (3 jobs) every option is a voice of the same donor staff, and in all 67 that staff has one word
  per beat in the question's bars. Two singers would never disagree on that choice.
- In one donor bar (asked for two lanes), voice 1 has a word on one beat and voice 2 has the next
  word on a later beat, and together they are one text. Whichever voice is picked, one of the two
  words is lost.

The same label problem reaches the same-staff path (SIG-170). A lane that picked one voice's words
(`lyric_line`) reads only that voice in every bar of the group, so a bar where the staff goes back
to one voice loses its word. Tested with that score's real voice ids and placeholder words:
that bar gets "+" (it holds the previous word) instead of its own word. This doesn't show up in the
corpus, because the one job with `lyric_line` questions asks each of them for a single bar. In that
job one option list also names a voice on a staff the lane doesn't read in that bar (1 bar,
first-option answers).

## 3. Terms

- **Lyric line**: the words of the requested verse typed under one staff, by beat, whatever voice
  each word is typed on. The file declares it (`<lyric number= name=>` on the staff's notes). The
  parser already keeps only the requested line's word on each note, using the same filter that
  synthesis uses, and `StaffLyricLine` already collects those words per staff. Nothing new is built.
- **Two-text bar**: a bar where the staff's voices sing different words. That means either
  - **clash**: two different words at one beat, or
  - **independent**: two or more voices each have a word on a beat where no other voice on the
    staff has a note (today's `independent_rhythm` reason).
- **One-text bar**: any other bar with words. Either one voice carries them, or one text is shared
  across voices.

## 4. The rule

```
text choice, for each bar of a staff's lyric line:
    one-text bar:   read every word in the bar, whatever voice it is typed on
    two-text bar:   read only the chosen voice's words
```

The bar kinds come from one function in `preprocess_sheet.py`, used by both the sheet and the
assembler.

### 4a. Borrowed words (the lane's staff has no words)

```
donor staves = staves of other, non-derived parts whose lyric line has words in the segment

options = for each donor staff:
    no two-text bar in the segment:     (staff)                                   # reads the whole line
    independent bars, no clash:         (staff) and (staff, voice) for each voice with words there
    a clash bar:                        (staff, voice) for each voice with words there

no donor staff → today's fallbacks, unchanged
one option     → fixed, no question
several        → ask lyric_source with these options; evidence per option as today
                 (word count, onset alignment with the lane's notes), ordered as today

lyric_strategy: asked as today, but "donor onsets" = the line's word beats after the text choice
```

A (staff, voice) option applies its voice only in that staff's two-text bars. In one-text bars the
lane still reads the whole line.

### 4b. Words on the melody's own staff (SIG-170 path): close the gap

- A chosen `lyric_line` voice limits only clash bars. In every other bar the lane reads the line as
  if no voice had been chosen.
- `lyric_line` options list only the voices that carry words in the clash bars of the staves this
  lane reads.
- `lyric_reading` (independent bars) keeps its question and options. `own_words_only` is that
  path's text choice; section 4c defines how it reads.

### 4c. Words on the melody's own staff: read the line, drop the per-bar carrier

**Today.** The plan names one voice per bar as the source of a lane's words (`_carriers` in the
assembler):

```
for each bar of the lane:
    carrier = the lane's melody voice, if it has words in this bar
              else the voice with the most words, then the lowest voice id
    a bar without words takes the previous bar's carrier
    strategy = staff_line    one text shared across voices, no word for the lane, or share_line
               strict_onset  everything else: one carrier, a clash, own_words_only, two staves
strict_onset copies the carrier's word at each lane note's beat
```

"Most words" is not a rule a singer would recognise; it exists only because the plan names one
voice per bar. Evidence that it matters:

- 5,074 of 5,172 lane-bars in the corpus are read through a carrier with `strict_onset`
  (40 jobs, first-option answers).
- 10 lane notes in 3 jobs are tied continuations that get a new word from the carrier. A held note
  can't start a new syllable.
- An `own_words_only` bar can follow the word counts instead of the answer. Bar 2 below: voice 1
  has words on beats 2 and 3, voice 3 has words elsewhere, and the lane (voice 2) rests on beats
  1–2 and enters on beat 3 with no word. The sheet asks `lyric_reading`; the answer is
  `own_words_only`. Run through today's code:

  | Voice with most words in bar 2 | Today | 4c |
  |---|---|---|
  | voice 1 (2 words against 1) | voice 1's word on beat 3 | voice 1's word on beat 3 |
  | voice 3 (3 words against 2) | no word; the plan fails (`action_required`) | voice 1's word on beat 3 |

**New.** Same-staff lanes write the line too, so `_carriers`, its tie-break and its "previous
bar's carrier" fill-in go:

```
for each bar of the lane:
    lyric_source      = the lane's melody voice           # only so today's checks resolve
    lyric_strategy    = staff_line
    lyric_source_line = {staff: null, voice_part_id: V}   # null staff: each note reads its own staff
    V = clash bar                         → the lyric_line voice
        independent bar, own_words_only   → the lane's own voice
        any other bar                     → null (the whole line)
```

How a lane note reads, after the filter V:

```
tied continuation                       → its own typed word, else "+"            (as today)
a word starts on its beat               → that word
no word on its beat:
    V is the lane's own voice, and the note comes after a rest or is the lane's first note:
        exactly one word starts on this beat anywhere on the staff → that word
    otherwise                           → "+" after an earlier word, else none     (as today)
```

Why the own-voice exception: a syllable can't be held across a rest, so a new note after a rest
needs a word. Engravers type a word once when voices start together, which is the same
convention the shared line relies on. Directly after the lane's own sung note, `own_words_only`
still holds ("+"), so the two answers differ exactly where a judgment is needed: is that
wordless note a melisma or a syllable shared with another voice?

**Not changed: holding across a rest in the whole-line reading.** With V = null, a note after a
rest with no word on its beat still gets "+". In the corpus that happens to 1 lane note today and
2 under 4c, out of 3,051. No word starts on those beats, so the only alternative is no word, which
the lyric check reports as missing. Left as it is; Alan's decision.

**Measured** (emulated: only bars that need no filter were switched to the line; clash and
`own_words_only` bars need the new filter and kept today's encoding):

| | Today | 4c, emulated |
|---|---|---|
| Jobs ready / ready with warnings / action required (37 that build a sheet) | 24 / 7 / 6 | 27 / 6 / 4 |
| Lane notes that change, in the 24 jobs ready both times | | 11 of 3,051: 10 tied continuations get "+" instead of a new word; 1 note after a rest gets "+" instead of none |

The 2 jobs that stop being `action_required` were failing the lyric check
(`validation_failed_needs_review`); no job's status gets worse.

## 5. Plan format and executor

Section fields, for borrowed lanes and same-staff lanes:

```
lyric_source:      {part_id, voice_part_id}   # borrowed: the donor staff's part and the chosen text
                                              # voice, else the first voice with words there;
                                              # same-staff: the lane's melody voice
                                              # (kept so today's checks still resolve)
lyric_source_line: {staff: "1" | null, voice_part_id: "voice part 3" | null}   # NEW
lyric_strategy:    borrowed: strict_onset | overlap_best_match | syllable_flow  (unchanged)
                   same-staff: staff_line
```

The assembler writes `lyric_source_line.voice_part_id` per bar: the chosen voice in two-text bars
(4a, 4c), `null` elsewhere. Equal bars merge into one section, as today. The executor never
classifies bars. It only filters by what the plan says.

```
executor, when a section has lyric_source_line:
    staff = lyric_source_line.staff, or each lane note's own staff when null
    source notes = notes of the lyric_source part on that staff that carry a word of the
                   requested verse
    if lyric_source_line.voice_part_id: keep only that voice's notes
                   (the whole staff stays available for 4c's own-voice exception)
    keep one note per beat (the chord tones carrying one word count once)
    run the section's fitting rule on these notes, as today
```

- The donor staff is named explicitly. `staff_line` today pairs staves by number only, so borrowing
  works only when both staves happen to be staff 1.
- A section without `lyric_source_line` behaves exactly as today. That covers v1.
- The plan checks that compare a donor voice with alternatives (the
  `_best_same_part_word_lyric_source_for_range` family: weak source, stronger local source,
  extension-only source, empty source) skip a line source, as two of them already skip
  `staff_line`. The existing after-split lyric check still checks the lane's words.
- v2 plans are not checked against the MCP tool schema; SIG-170's `staff_line` is outside that
  schema's enum and its plans run. So the schema doesn't change.

## 6. Prompt

The `lyric_source` line already says "Pick the staff whose words fit this lane's notes best". Add
one sentence: an option that names a voice means the staff sings two texts in some of these bars,
and the option reads that voice's words there.

## 7. Where

| File | Change |
|---|---|
| `src/api/preprocess_sheet.py` | bar-kind function; `_lyric_options` offers donor staves (and staff + voice for two-text staves); `read_lane_lyrics` and `_lyric_line_options` apply the text choice |
| `src/api/preprocess_assembler.py` | every lane: write `lyric_source_line` per bar (4a, 4c); remove `_carriers` |
| `src/api/voice_parts.py` | line-source notes for `_propagate_lyrics`, the voice filter, a null staff, and 4c's own-voice exception after a rest; skip the donor-voice comparison checks for a line source |
| `src/backend/config/system_prompt_planner_v2.txt` | one sentence (section 6) |
| `scripts/replay/prove_sheet_construction.py` | print `lyric_source_line` in the offline proof |

## 8. Tests

Each test fails on today's code unless marked.

- Two parts. Staff A carries the words, all on voice 1, in bars with a second voice and in bars
  without one. A lane on staff B borrows: one fixed source, no question, words in every bar.
- A donor bar with a word on voice 1 at beat 1 and on voice 2 at beat 3 (one text): the lane gets
  both words.
- A donor staff with a clash bar: options are (staff, voice) for each voice. The lane gets the chosen
  voice's word there and the whole line in the other bars.
- Two donor staves, both one-text: asked, one option per staff.
- Same-staff: a lane picks voice 2 in a clash bar; the next bar has one voice with a word. The lane
  gets that word (today it gets "+").
- `lyric_line` options list only voices with words in the clash bars of the staff the lane reads.
- Same-staff, a tied continuation in the lane where another voice starts a word on that beat: the
  lane holds "+" (today it gets the other voice's word).
- Same-staff, `own_words_only`, the lane enters after a rest with no word where exactly one other
  voice starts a word: the lane gets that word, whichever voice has more words in the bar (today:
  that word, or no word and `action_required`, depending on the counts).
- Same-staff, `own_words_only`, a wordless lane note directly after the lane's own sung note: "+",
  even when another voice starts a word on that beat. With `share_line` the same note gets that word.
- Same-staff, a lane whose melody sits on two staves in one bar: each note reads its own staff.
- Same-staff, `share_line` and one-text bars: the same words as today (passes today).
- Executor: a section with `lyric_source_line` on donor staff 1, lane on staff 2, copies words by
  beat. A section without the field is unchanged (passes today).
- v1, offline proof on the 40 jobs: plans and executor results identical (passes today).

## 9. Proof

- Offline, 40 jobs, v2:
  - Borrowed-word questions go from 88 to about 21: the 67 one-staff questions become fixed, and the
    4 mixed questions become a staff choice.
  - Total questions go from 745 to about 678.
- Executor results change in the 7 jobs with borrowed words and in same-staff lanes (4c). Every
  changed lane must have equal or higher word coverage; list any lane where it is lower, and every
  changed note by kind (word, "+", none). The emulation in 4c predicts 4 jobs with a better status,
  none worse, and 11 changed notes in jobs ready both times.
- v1: identical.
- Full suite: the same baseline failures.
- On approval:
  - replay the 3 jobs whose questions disappear, 2 runs each;
  - run the local two-part score end to end on the local stack.

## 10. Out of scope

- The parser keeping the written voice number next to its label. That is a wider change; it would
  affect v1 and every lane rule built on the shared line.
- A second voice's words typed on lyric line 2 instead of line 1. The corpus has 0 such staves.
- Holding "+" across a rest in the whole-line reading (see 4c): unchanged, 1–2 notes in the corpus.
- The number of `lyric_strategy` and `rank_fallback` questions.
- The bass lane resting in forward gaps.
