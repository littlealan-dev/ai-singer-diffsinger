# Music21 Integration & Lyric Transfer Plan

## Goal
Replace insecure raw Python execution with a structured, safe system using `music21` to modify MusicXML scores. A key feature is the ability to automatically transfer lyrics from a source part (e.g., Soprano) to other parts (Alto, Tenor, Bass) to ensure the AI Singer generates audio for all voices.

## Architecture
We will integrate `music21` directly into the existing MCP server process (`src/mcp`).
- **New Module**: `src/mcp/music21_tools.py` will contain the logic.
- **Dependency**: Add `music21` to `pyproject.toml`.

## Lyric Transfer Algorithm
This algorithm solves the problem where lyrics exist only on one part (e.g., Soprano) but are needed for all parts.

### Logic
The transfer is based on **timing coincidence**. If a note in the target part starts at the exact same time as a note in the source part that has lyrics, we copy the lyrics.

1. **Input**:
   - `score`: The music21 Score object.
   - `source_id`: Part ID of the part containing lyrics (e.g., "P1").
   - `target_ids`: List of Part IDs to receive lyrics (e.g., ["P2", "P3"]).

2. **Analysis Phase**:
   - Iterate through the `source_part` flattened notes.
   - Build a **Lyric Map**: A dictionary mapping numerical offsets (start times) to `Lyric` objects.
     ```python
     lyric_map = {
         0.0: ["Hel-"],
         1.0: ["lo"],
         # Only map offsets where lyrics actually exist
     }
     ```

3. **Transfer Phase**:
   - For each `target_part` in `target_ids`:
     - Iterate through its notes (flattened).
     - For each note, check its `offset` (start time).
     - **IF** `offset` exists in `lyric_map`:
       - **AND** the target note has no existing lyrics (optional safety check):
       - Copy the text and syllabic properties (begin/middle/end) from the map to the target note.
     - **ELSE**: Leave the note as is (melismatic passages or rests).

### Edge Cases
- **Melisma**: If one syllable stretches over multiple notes in the source, it's attached to the first note. The target part might have moving notes during that syllable. *Current Strategy*: Copy only to the simultaneous attack point.
- **Rhythmic Mismatch**: If the target part starts a note when the source is holding a note or resting, no lyric is transferred.

## Exposed MCP Tools

### `transfer_lyrics`
- **Arguments**: `source_part_id` (string), `target_part_ids` (list of strings).
- **Description**: Copies lyrics to target parts by matching note start times.

### `transpose_score`
- **Arguments**: `interval` (integer semitones).
- **Description**: Transposes the entire score up or down.

### `analyze_key`
- **Arguments**: None.
- **Description**: Returns the estimated key of the piece.

## Implementation Steps

1.  **Add Dependency**: `music21`.
2.  **Create Module**: `src/mcp/music21_tools.py` with `transfer_lyrics` function implementing the map-based algorithm.
3.  **Register Tool**: Update `src/mcp/tools.py` to expose `transfer_lyrics`.
4.  **Test**: Create `tests/mcp/test_music21_logic.py` with a sample score to verify lyrics appear on the target part.
