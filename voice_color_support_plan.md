# Voice Color Support Plan (LLM-guided)

## Goal
Enable LLM-guided selection of a voice color (style) at synthesis time, using the voicebank's available subbanks. The LLM should only choose from valid options, defaulting to the standard color when none is specified.

## Key Findings From OpenUtau
- Voice colors are defined as **subbanks** with a `color` name and `suffix`.
- The CLR expression on notes selects a color option by index; render uses the subbank suffix to pick samples / embeddings.
- For DiffSinger, the suffix maps to a speaker embedding entry.

## Proposed User / LLM Flow
1. Backend exposes voicebank + subbank color metadata to the LLM.
2. User can request a style in natural language ("soft", "strong", "bright").
3. LLM maps the request to the closest **valid** voice color option for the selected voicebank.
4. LLM calls `synthesize` with an optional `voice_color` parameter.
5. Backend validates `voice_color` and applies it globally; if absent, use the default color.

## Data Extraction (Voicebank Metadata)
- Source of truth: `character.yaml` for `subbanks` entries.
  - Example fields: `color` (display name), `suffix` (used for speaker embed lookup).
- Optional cross-check: `dsconfig.yaml` `speakers` list should include the same suffixes.
- Expose the metadata in `get_voicebank_info` (or a new API field) as:
  - `voice_colors`: list of `{ name, suffix }`.
  - `default_voice_color`: string (first subbank, or the one containing "normal" / "standard" if present).

## Backend Changes (Planned)
1. **Tool schema**: add optional `voice_color` parameter to `synthesize`.
2. **Validation**:
   - If `voice_color` is provided, it must match a known color name for the selected voicebank.
   - If invalid or unsupported, silently ignore and fall back to default.
3. **Defaulting**:
   - If `voice_color` is not provided, use `default_voice_color`.
4. **Synthesis pipeline**:
   - Apply the selected color globally (not per-note).
   - For DiffSinger, map `voice_color` -> subbank `suffix` -> speaker index (from `dsconfig.yaml` `speakers`).

## LLM Prompt Update
- Add a short section listing available voicebanks and their voice colors.
- Instruct the LLM:
  - Choose a `voice_color` only from the provided list.
  - If the user gives a style request, map it to the closest valid option.
  - If no style is requested, omit `voice_color` and let the backend default it.

## Testing Plan
- Unit: `get_voicebank_info` returns `voice_colors` and `default_voice_color`.
- API: `synthesize` accepts a valid `voice_color` and ignores invalid values.
- E2E: prompt with "soft" results in `voice_color: "02: soft"` (for Raine Rena).

## Decisions
- **Exact match only**: treat `subbanks.color` as the ID; no partial matching.
- **No alias list**: LLM must choose from the provided color IDs directly.
- **No subbanks behavior**: follow OpenUtau semantics; ignore `voice_color` and use the default voice (empty suffix).

## Open Questions
- None. For unsupported colors, we will silently ignore and fall back to default (OpenUtau behavior).
