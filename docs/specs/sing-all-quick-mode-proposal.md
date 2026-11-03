# "Sing All", quick mode: one tap, sensible defaults, a quote, Confirm

Status: proposal for Alan's review (2026-10-23). No prototype yet.
Builds on `sing-all-quote-ui-mockups.md`: the 3-step wizard, and "Sing All (2)", the one-screen modal.

## 1. Who it's for

Someone who:
- downloads a score from the web (a hymn, a pop arrangement, a school choir piece);
- wants to hear the whole song sung;
- doesn't know or care what solfège, verses, repeats, part splitting or part-dominant mixes are.

They should be able to press one button, see the price, and confirm. Every choice the wizard asks about still exists, but stays hidden until the user asks to customise.

## 2. What "everything default" means

| Setting | Quick-mode default | Why |
|---|---|---|
| Parts | **All vocal parts**, split automatically where two share a staff | "Sing the song" means all of it |
| Sing in | **Lyrics only** | Solfège is a learning feature; this user wants the song |
| A part with no lyrics | **Sung in solfège** (decided 2026-10-23), with one line in the summary saying so | The part can't be sung on words |
| Verses | **Verse 1** (decided 2026-10-23, see §2.1) | Until the engine can sing verse n on repeat pass n with correct part and lyric copying |
| Repeats | **With repeats** | That's how the song is performed |
| Voices | **The recommended voice per part**, the existing range-based logic | No choice needed |
| Style | **Default voice style**, no notes | — |
| Mixes | **One full mix only** (decided 2026-10-23): every part together, with the instrumentals | One song to listen to |
| Always included | A solo track per part in the player, and the instrumental MIDI | As in the other designs |
| Download | **The same zip as customised mode** (decided 2026-10-23): each part solo, the instrumental MIDI, the full mix, plus the README and voicebank credits | One consistent package |

### 2.1 Verses and repeats

Decided 2026-10-23: quick mode sings **verse 1, with repeats**. A repeated section repeats verse 1 too.

Singing verse n on repeat pass n, with the part and lyric copying right, comes later as its own feature. Until then, a score with several verses gets the summary line "verse 1 of 3", and other verses are picked under Customise.

## 3. The screen

Same modal as "Sing All (2)". Collapsed by default, it shows only the summary and the quote.

```text
┌───────────────────────────────────────────────────────────┐
│ Sing All                                                ✕  │
│ Amazing Grace                                              │
│                                                            │
│ 4 parts · lyrics · verse 1 of 3 · about 0:59      │
│ Voices: Qixuan, LIEE, Printto, Apollo                      │
│                                                            │
│ ┌────────────────────────────────────────────────────────┐ │
│ │ Singing 4 parts (about 0:59 each)             8 cr     │ │
│ │ Preparing the parts (splitting)             TBD        │ │
│ │ Full mix                                    TBD        │ │
│ │ Instrumentals                                1 cr      │ │
│ │ Total                           9 cr + TBD             │ │
│ │ Balance                        42 → 33                 │ │
│ └────────────────────────────────────────────────────────┘ │
│                                                            │
│ ▸ Customise                                                │
│                                                            │
│                          [ Sing it · 9 cr ]                │
└───────────────────────────────────────────────────────────┘
```

**The summary line** describes the defaults in plain words. It never says "solfège", "repeats" or "part-dominant" unless the user customises, with one exception: a part with no lyrics gets a short note, e.g. "Bass has no words, so it's sung in solfège (do-re-mi)".

**The quote** has 4–5 lines, in plain words:
- "Singing 4 parts" rather than "Vocal takes";
- "Preparing the parts" rather than "Part splitting", shown only when a split is needed;
- "Full mix" with a (i) explaining it.

**The button** says "Sing it". On a free plan it says "Upgrade to sing", as in the other designs.

## 4. Customise

**▸ Customise** expands, inside the same modal, the existing controls from "Sing All (2)":
- **What to sing:** Sing in (Lyrics / Solfège / Both), Verse (which, or all), and the parts table (include, voice).
- **How to sing:** Mixes (Part-dominant / Full mix / Both), Repeats, and style notes.

Rules:
- Every change updates the summary line and the quote straight away.
- When the user changes anything, a **"Reset to defaults"** link appears next to Customise.
- Collapsing Customise keeps the changes. The summary line then shows what's changed, e.g. "lyrics + solfège · 2 parts".

## 5. Flow

```text
user taps "Sing All" (or says "sing the whole song")
  → the app reads the score (parts, lyrics, verses, repeats, length): no LLM, no cost
  → the quick screen opens with the defaults and the quote
  → "Sing it"     → same job as today's Sing All confirm
  → "Customise"   → the controls expand; the quote updates live
  → ✕ / Esc       → nothing happens; the chat bubble keeps a "Sing All · ready · 9 cr" card with Open
```

There's no LLM call before Confirm. The defaults, the summary line and the quote all come from the parsed score. The only LLM steps are:
- the part split, if needed, which runs after Confirm;
- style notes, and only if the user typed any under Customise.

## 6. Edge cases

| Case | What quick mode does |
|---|---|
| One vocal part only | Summary "1 part"; no "Full mix" line; the download is that part with the instrumentals |
| No instrumentals in the score | No instrumentals line; the full mix is voices only |
| No lyrics anywhere | Summary "no words in this score, so it's sung in solfège (do-re-mi)"; the quote is unchanged |
| A part the splitter can't separate | As decided for Sing All: pause and re-quote after the split |
| A very long song (over the length limit) | The quote shows the length and its cost, and a short line suggests Customise (fewer verses, or without repeats) |
| Not enough credits | Total in red, with "Top up" or "Upgrade" in place of "Sing it" |

## 7. How it relates to the other two designs

| | Wizard (3 steps) | Sing All (2), one screen | **Quick mode** |
|---|---|---|---|
| First thing the user sees | Step 1: the parts table | Every setting + the quote | **A summary line + the quote** |
| Taps to confirm with defaults | 3 | 1 (after scrolling) | **1** |
| Customising | Built into every step | Always visible | **Behind "Customise"**, the same controls as Sing All (2) |
| Words like solfège / repeats / part-dominant | Visible | Visible | **Hidden until Customise** |

Proposal: make quick mode **the** Sing All entry. Customise expanded is effectively Sing All (2), so the two become one screen with two states. The wizard stays only as a reference prototype.

## 8. Decisions (Alan, 2026-10-23)

1. Verses: **verse 1, with repeats** (repeats repeat verse 1), until verse-per-repeat-pass is supported.
2. A part with no lyrics: **solfège**.
3. Mixes: **only the full mix** by default.
4. Download: **the full zip**: each part solo, the MIDI, the full mix, the README and the credits.
5. **One button, one entry, two states:** quick and customise. No modal on top of the modal, and the customise state must fit one screen height. Options are in §9.

## 9. Two states in one modal: options

Constraints, from Alan:
- one button and one modal;
- no modal on top of a modal;
- the customise state must fit in one screen height without scrolling: about 800 px on a phone, about 870 px in the desktop modal.

### Option A: the summary chips are the controls

The summary line is made of tappable chips. Tapping one opens a small dropdown anchored under it, inside the same modal (not a dialog). The quote stays where it is and updates live.

```text
Amazing Grace
[4 parts ▾] [Lyrics ▾] [Verse 1 ▾] [With repeats ▾] [Full mix ▾] [+ Style]
                ┌──────────────┐
                │ ● Lyrics      │
                │ ○ Solfège     │
                │ ○ Both        │
                └──────────────┘
Singing 4 parts            8 cr
…
Total             9 cr + TBD          [ Sing it · 9 cr ]
```

- **Good:** there is no separate customise state at all, and it's the smallest possible change from quick mode.
- **Weak:** "4 parts ▾" needs a bigger dropdown (parts plus a voice per part). The chips also show the same jargon ("Lyrics", "With repeats") that quick mode hides.

### Option B: the modal turns the page

"Customise" slides the modal's content sideways to a settings page, with "← Back" and "Done", like navigation inside an iOS sheet. The quote shrinks to a total bar at the bottom. "Done" slides back to the quick page, with the summary and quote updated.

- **Good:** a clean page for the settings, and the quick page stays simple.
- **Weak:** the user can't see the line items while changing settings, only the total.

### Option C (recommended): expand in place, as a compact settings list

"Customise" turns the summary line into a list of one-line rows, inside the same modal and with no page change. The full quote folds into a sticky total bar, which a tap opens again.

**Quick state** (as in §3):

```text
┌ Sing All ─────────────────────────────── ✕ ┐
│ Amazing Grace                               │
│ 4 parts · lyrics · verse 1 of 3 · 0:59      │
│ Voices: Qixuan, LIEE, Printto, Apollo       │
│ ┌─────────────────────────────────────────┐ │
│ │ Singing 4 parts (0:59 each)       8 cr  │ │
│ │ Preparing the parts               TBD   │ │
│ │ Full mix                          TBD   │ │
│ │ Instrumentals                     1 cr  │ │
│ │ Total                    9 cr + TBD     │ │
│ └─────────────────────────────────────────┘ │
│ ▸ Customise                                 │
│                        [ Sing it · 9 cr ]   │
└─────────────────────────────────────────────┘
```

**Customise state** (the same modal; "Customise" became "▾ Customise · Reset"):

```text
┌ Sing All ─────────────────────────────── ✕ ┐
│ Amazing Grace                               │
│ ▾ Customise                       Reset     │
│ Parts & voices    4 of 4 · Qixuan, …     ▸  │   ← tap: rows expand right here
│ Sing in           [Lyrics|Solfège|Both]     │
│ Verse             [ 1 ▾ ]                   │
│ Repeats           [ On | Off ]  0:59 / 0:29 │
│ Mixes ⓘ           [Full|Part-dominant|Both] │
│ Style notes       Add ▸                     │   ← tap: a 2-line text box opens here
│─────────────────────────────────────────────│
│ Total 9 cr + TBD  (+4 cr)   ▸ details       │   ← sticky; "+4 cr" flashes on a change
│                            [ Sing it · 9 cr ]│
└─────────────────────────────────────────────┘
```

How the quote behaves:
- **In the quick state,** it's the main content: every line visible.
- **In the customise state,** it folds to one sticky line: the total, a short-lived change badge ("+4 cr", "−2 cr") after each change, and "▸ details".
  - "▸ details" opens the line items as a tray at the bottom of the same modal, over the settings rows. It isn't a new dialog, and tapping it again or swiping it down closes it.
- **"▾ Customise"** (tapping the header) folds the list back to the quick state. The summary line then describes the new choices, e.g. "2 parts · lyrics + solfège · verse 2 · 0:29".
- **"Sing it"** stays in the same place in both states.

Height check for the customise state:

| Block | Height |
|---|---|
| Header + title | ~90 px |
| 6 setting rows × 44 px | ~264 px |
| Sticky total + button | ~90 px |
| **Rows collapsed** | **~450 px** |
| + Parts & voices expanded (4 parts × 40 px + instrumentals) | +200 → ~650 px |
| + Style notes open (2 lines) | +70 → **~720 px**: fits a phone (~800) and the desktop modal (~870) |

A score with 6–8 parts could push past one screen with Parts expanded. Then only the parts rows scroll, inside their own box (max ~240 px), and the page itself still doesn't scroll.

Why C:
- quick and customise are visibly the same screen, just opened up, so there's one entry and two states;
- no dialog on top of a dialog;
- the total is always in view while settings change, and the line items are one tap away;
- it reuses the controls from Sing All (2), as rows instead of sections.
