# SightSinger.ai

AI sight-singing from MusicXML, via AI chat. No DAW required.

## What it’s for

- **Indie Songwriters**: instant vocal demos without a session singer or professional DAW mockup.
- **Choir & Worship Leaders**: quick SATB parts or melody practice tracks.
- **Beginner Singers**: try singing with a score-accurate guide before investing in lessons.
- **Quick Song Learners**: learn a few songs fast without diving into theory or breath training.

## How it works

1. Upload MusicXML
2. Tell the AI how would you like to sing
3. AI interprets your request and calls the API through MCP server to synthesize the audio
4. Chat to iterate quickly, render new takes, and share the demo with others

## SightSinger.ai is NOT

- A DAW replacement
- A human vocalist
- A song generator (text-to-music)
- A voice converter

## What’s in this repo

```
ui/                      # React/Vite frontend
src/backend/             # FastAPI backend + orchestration
src/api/                 # DiffSinger pipeline (parse/synthesize)
src/mcp_server.py        # MCP server (stdio JSON-RPC)
assets/voicebanks/       # Local voicebanks for dev
tests/                   # End-to-end and unit tests
```

## Local development

Backend:
```bash
scripts/start_backend_dev.sh
```

Frontend:
```bash
scripts/start_frontend_dev.sh
```

URLs:
- Landing: `http://localhost:5173/`
- Demo: `http://localhost:5173/demo`
- Backend: `http://localhost:8000`

## Demo assets (public)

Scripted demo uses local assets:
```
ui/public/landing/demo/scores/amazing-grace.mxl
ui/public/landing/demo/audio/amazing-grace-soprano.mp3
ui/public/landing/demo/audio/amazing-grace-tenor.mp3
```

## Credits

Voicebank credits live in `CREDITS.md`.

## Voicebanks

Dev (local):
- Place voicebanks under `assets/voicebanks/`

Relevant env:
```
VOICEBANK_BUCKET
VOICEBANK_PREFIX=assets/voicebanks
VOICEBANK_CACHE_DIR=/tmp/voicebanks
```

## MCP Server (stdio JSON-RPC)

Start:
```bash
.venv/bin/python -m src.mcp_server --device cpu
```

Notes:
- Voicebank inputs are **IDs only**.
- Logs go to stderr in MCP mode.

Example call sequence:
```json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"parse_score","arguments":{"file_path":"assets/test_data/amazing-grace-satb-verse1.xml"}}}
```

## Tests

End-to-end synthesis:
```bash
.venv/bin/python -m pytest tests/test_end_to_end.py -k test_full_synthesis -vv
```


## Docs

- `architecture.md` – current system architecture
- `deployment_architecture.md` – deployment design and ops notes
- `api_design.md` – API design spec

## License

MIT. See `LICENSE`.
