# ai-singer-diffsinger

AI LLM sight-read singer that reads MusicXML and synthesizes singing voice via a DiffSinger pipeline.

## MCP Server (stdio JSON-RPC)

The MCP server exposes the backend APIs as JSON-RPC methods over stdio.

### Start the server
```bash
.venv/bin/python -m src.mcp_server --device cpu
```

Notes:
- `--device` is a server startup option and is not exposed to MCP tools.
- Voicebank inputs are **IDs only** (directory names under `assets/voicebanks`).

### Request/response basics

Initialize:
```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"1.0"}}
```

List tools:
```json
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
```

Call a tool:
```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_voicebanks","arguments":{}}}
```

Responses follow JSON-RPC 2.0 and return results in `result`. Tool errors return:
```json
{"error":{"message":"...","type":"ValueError"}}
```

## Example: End-to-End Synthesis

1) `parse_score`:
```json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"parse_score","arguments":{"file_path":"assets/test_data/amazing-grace-satb-verse1.xml"}}}
```

2) `synthesize` (voicebank ID only):
```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"synthesize","arguments":{"score":{"...":"..."},"voicebank":"Raine_Rena_2.01","voice_id":"soprano"}}}
```

3) `save_audio` (returns base64 WAV/MP3 bytes):
```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"save_audio","arguments":{"waveform":[0.0,0.1,-0.1],"output_path":"tests/output/mcp_out.wav","sample_rate":44100}}}
```

## Tool Summary

Pipeline + utilities exposed via MCP:
- `parse_score`
- `modify_score`
- `phonemize`
- `align_phonemes_to_notes`
- `predict_durations`
- `predict_pitch`
- `predict_variance`
- `synthesize_audio`
- `save_audio` (returns `audio_base64`)
- `synthesize`
- `list_voicebanks` (returns IDs + names)
- `get_voicebank_info` (voicebank ID only)

## Development

Run MCP unit tests:
```bash
.venv/bin/python -m unittest tests.test_mcp_server
```

Run MCP end-to-end test:
```bash
.venv/bin/python -m unittest tests.test_mcp_end_to_end
```
