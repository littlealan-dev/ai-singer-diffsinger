# MCP Wrapper Implementation Plan

This plan describes how to expose the existing SVS APIs as MCP tools so an LLM can call them directly.

---

## Goals

- Wrap the current Python APIs in `src/api/*` as MCP tools with typed JSON input/output.
- Preserve the step-by-step workflow from `api_design.md`.
- Keep file I/O safe and explicit (no arbitrary filesystem access).
- Provide clear errors, logging, and observability.

---

## Scope (Tools to Expose)

Public MCP tools (from `api_design.md`):
- `parse_score`
- `modify_score`
- `phonemize`
- `align_phonemes_to_notes`
- `predict_durations`
- `predict_pitch`
- `predict_variance`
- `synthesize_audio`
- `save_audio`
- `synthesize`
- `list_voicebanks`
- `get_voicebank_info`

Internal-only (not exposed as MCP tools):
- `synthesize_mel`
- `vocode`

---

## Design Decisions

- **Tool inputs/outputs**: JSON-serializable only. Use lists for arrays, avoid numpy in tool boundary.
- **Paths**: Limit all filesystem access to project-root-relative paths.
- **Defaults**: Keep current defaults from API functions (e.g., `device="cpu"`).
- **Security**: Keep `modify_score` sandboxed; disallow imports and file/network access.
- **Voicebank lookup**: Accept IDs only (directory names). Resolve IDs to paths via `list_voicebanks`.
- **Device selection**: Do not expose `device` in MCP; keep it as an internal startup argument.
- **save_audio output**: Return base64-encoded audio bytes instead of file paths.

---

## Implementation Steps

### 1) Choose MCP Server Framework
- Pick the MCP server runtime used by this project (likely Python).
- Add a minimal server entrypoint, e.g. `src/mcp_server.py`.

### 2) Define Tool Schemas
- For each tool, define:
  - Name, description
  - JSON schema for inputs
  - JSON schema for outputs
- Keep schema aligned with `api_design.md`.

### 3) Build Tool Handlers
- Map each MCP tool to its corresponding function in `src/api`.
- Convert types as needed:
  - `Path` ⇄ `str`
  - `np.ndarray` ⇄ `List[float]`
- Add structured error handling:
  - Return `{"error": {"message": "...", "type": "FileNotFoundError"}}` on failures.

### 4) Voicebank Resolution & Validation
- Implement a shared resolver:
  - Require `voicebank` to be an ID (directory name).
  - Resolve via `list_voicebanks` and reject unknown IDs.

### 5) Configure Runtime
- Add config file (optional) for:
  - Default voicebank
  - Allowed directories (assets, outputs)
  - Device selection (`cpu`/`cuda`) as internal startup config
- Provide env overrides (internal only, not exposed via MCP).

### 6) Logging & Observability
- Log tool name, inputs (redacted if needed), and timing.
- Emit step timing for heavy tools (`synthesize`, `synthesize_audio`).

### 7) Tests
- Unit tests for:
  - Tool schema validation
  - Voicebank resolution
  - Error mapping
- Integration tests to:
  - Call MCP server tools end-to-end with a small MusicXML file.

---

## File Layout (Proposed)

- `src/mcp_server.py` — MCP server entrypoint
- `src/mcp/tools.py` — tool registry + schemas
- `src/mcp/handlers.py` — tool handler functions
- `src/mcp/resolve.py` — voicebank/path resolution helpers
- `tests/test_mcp_tools.py` — schema + handler tests
- `tests/test_mcp_integration.py` — end-to-end MCP tests

---

## Rollout Checklist

- [ ] Implement MCP server entrypoint
- [ ] Register tools with schemas
- [ ] Validate all tool inputs/outputs are JSON-only
- [ ] Ensure sandboxing in `modify_score`
- [ ] Add integration tests
- [ ] Update README with MCP usage examples

---

## Open Questions

- Is returning base64 for `save_audio` sufficient, or do we also need to persist files internally?
