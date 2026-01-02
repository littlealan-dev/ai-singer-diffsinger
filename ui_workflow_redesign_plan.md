# UI Workflow Redesign Plan: Verse + Part Selection

## Goal
Improve the user flow after score upload so the system explicitly selects the vocal part and verse before synthesis. This avoids incorrect part selection (e.g., piano) and incorrect verse usage.

## Current Issues
- Default part selection can pick a non-vocal part (e.g., piano), leading to no lyrics and short/incorrect audio.
- Only a single lyric line is available in the parsed JSON, so verse selection is not possible post-parse.
- The UI immediately allows chat/synthesis without clarifying the target part/verse.

## Proposed UX Flow
1. **Upload Score**
2. **Backend parses score and returns a summary** (metadata, parts, available verses).
3. **LLM asks the user** to choose:
   - Part (Soprano/Alto/Tenor/Bass or part name)
   - Verse number (1, 2, 3...)
4. **User responds**
5. **LLM calls synthesize** with explicit `part_id` (or `part_index`) and `verse_number`.

## Backend Changes (Planned)
### 1) Parse Summary
Generate summary with `music21` directly (no extra JSON fields needed) and return it after upload.

Add a summary response to upload or a new endpoint:
- Option A: Extend `POST /sessions/{id}/upload` response with `score_summary`
- Option B: Add `GET /sessions/{id}/summary`

Summary fields:
- `title`, `composer`, `lyricist`, `copyright`
- `parts`: `part_id`, `part_name`, `has_lyrics`, `voice_count`
- `available_verses`: list of lyric numbers found (e.g., `[1, 2]`)
- (optional) `measure_count`, `approx_duration_seconds`

### 2) Verse Selection Support (music21-first)
To avoid re-parsing and avoid storing all lyrics in JSON:
- Parse once with `music21` on upload and **cache the Score object in memory** (per session).
- Generate `score_summary` directly from the cached `music21` Score (parts, verses).
- When the user selects a verse/part, **derive a verse-specific JSON score on demand** from the cached Score (no reparse).

Notes:
- The cached Score should be evicted with the session to avoid memory leaks.
- If the backend restarts, cached Scores are lost; the system should fall back to re-parsing from file.

### 3) Synthesis Parameters
Extend synthesize to accept:
- `part_id` (preferred over `part_index`)
- `verse_number`

If `verse_number` is provided, the backend should build a verse-specific score JSON from the cached `music21` Score and replace `current_score` before synthesis.

## Frontend Changes (Planned)
- After upload, request and store `score_summary`.
- Display a compact summary in the chat (or a UI card) for the LLM/user.
- Add a lightweight “selection state” panel:
  - Part dropdown (from `parts`)
  - Verse dropdown (from `available_verses`)
- Allow “Let the LLM decide” as a default path, but ensure explicit user confirmation.

## LLM Prompting Changes (Planned)
Provide the LLM with:
- `score_summary`
- Guidance: ask user to pick part/verse when ambiguous
- Constraint: never synthesize without explicit part + verse selection

Tool calls should include:
- `synthesize` with `part_id` and `verse_number`
- If reparse is needed: a new tool like `parse_score` with `verse_number` (or `select_verse`)

## Data Model Updates (Planned)
### Score Summary Example
```json
{
  "title": "O Holy Night",
  "composer": "Adolphe Adam",
  "parts": [
    {"part_id": "P1", "part_name": "Sopraan", "has_lyrics": true},
    {"part_id": "P5", "part_name": "Piano", "has_lyrics": false}
  ],
  "available_verses": [1, 2]
}
```

## Edge Cases
- Scores with lyrics only in one part: offer automatic lyric transfer or restrict to that part.
- Missing verse numbers: treat as verse 1.
- Multiple voices inside a part: decide voice selection policy (highest pitch with lyrics, or voice=1).
 - Backend restarts: cached Score is lost, so summary/verse selection must re-parse from disk.

## Rollout Plan
1. Add summary extraction and return it after upload.
2. Update LLM prompt to ask for part/verse selection.
3. Add minimal UI selectors and send chosen values to backend.
4. Extend synthesize to accept explicit part + verse.

## Open Questions
- Is it acceptable to cache the music21 Score in memory per session for verse selection?
- Do we want to allow “auto” part selection as a fallback if the user refuses to pick?
