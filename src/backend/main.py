from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, AsyncIterator
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.backend.config import Settings
from src.backend.llm_factory import create_llm_client
from src.backend.mcp_client import McpRouter, McpError
from src.backend.orchestrator import Orchestrator
from src.backend.session import SessionStore


class ChatRequest(BaseModel):
    message: str


def create_app() -> FastAPI:
    settings = Settings.from_env()
    sessions = SessionStore(
        project_root=settings.project_root,
        sessions_dir=settings.sessions_dir,
        ttl_seconds=settings.session_ttl_seconds,
        max_sessions=settings.max_sessions,
    )
    router = McpRouter(settings)
    llm_client = create_llm_client(settings)
    orchestrator = Orchestrator(router, sessions, settings, llm_client)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.sessions_dir.mkdir(parents=True, exist_ok=True)
        router.start()
        try:
            yield
        finally:
            router.stop()

    app = FastAPI(title="SVS Backend", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.router = router
    app.state.llm_client = llm_client
    app.state.orchestrator = orchestrator

    @app.post("/sessions")
    async def create_session(request: Request) -> Dict[str, str]:
        session = await request.app.state.sessions.create_session()
        return {"session_id": session.id}

    @app.post("/sessions/{session_id}/upload")
    async def upload_musicxml(
        session_id: str,
        request: Request,
        file: UploadFile = File(...),
    ) -> Dict[str, Any]:
        sessions: SessionStore = request.app.state.sessions
        settings: Settings = request.app.state.settings
        await _get_session_or_404(sessions, session_id)

        original_name = Path(file.filename or "").name
        suffix = Path(original_name).suffix.lower()
        if suffix not in {".xml", ".mxl"}:
            raise HTTPException(status_code=400, detail="Only .xml or .mxl files are supported.")

        session_dir = sessions.session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        target_path = session_dir / f"score{suffix}"
        await _write_upload(target_path, file, settings.max_upload_bytes)

        await sessions.set_file(session_id, "musicxml_path", target_path)
        if original_name:
            await sessions.set_metadata(session_id, "musicxml_name", original_name)

        rel_path = str(target_path.relative_to(settings.project_root))
        try:
            score = await asyncio.to_thread(
                request.app.state.router.call_tool,
                "parse_score",
                {"file_path": rel_path, "expand_repeats": False},
            )
        except McpError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        version = await sessions.set_score(session_id, score)
        return {
            "session_id": session_id,
            "parsed": True,
            "current_score": {"score": score, "version": version},
        }

    @app.post("/sessions/{session_id}/chat")
    async def chat(session_id: str, request: Request, payload: ChatRequest) -> Dict[str, Any]:
        sessions: SessionStore = request.app.state.sessions
        orchestrator: Orchestrator = request.app.state.orchestrator
        await _get_session_or_404(sessions, session_id)
        try:
            return await orchestrator.handle_chat(session_id, payload.message)
        except McpError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/sessions/{session_id}/audio")
    async def get_audio(session_id: str, request: Request) -> FileResponse:
        sessions: SessionStore = request.app.state.sessions
        settings: Settings = request.app.state.settings
        snapshot = await _get_snapshot_or_404(sessions, session_id)
        current_audio = snapshot.get("current_audio")
        if not current_audio:
            raise HTTPException(status_code=404, detail="No audio available for this session.")
        audio_path = settings.project_root / current_audio["path"]
        if not audio_path.exists():
            raise HTTPException(status_code=404, detail="Audio file not found.")
        media_type = "audio/wav" if audio_path.suffix.lower() == ".wav" else "application/octet-stream"
        return FileResponse(audio_path, media_type=media_type, filename=audio_path.name)

    return app


async def _get_session_or_404(sessions: SessionStore, session_id: str) -> Any:
    try:
        return await sessions.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Session not found.") from exc


async def _get_snapshot_or_404(sessions: SessionStore, session_id: str) -> Dict[str, Any]:
    try:
        return await sessions.get_snapshot(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Session not found.") from exc


async def _write_upload(path: Path, file: UploadFile, max_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with path.open("wb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail="Upload too large.")
                handle.write(chunk)
    except HTTPException:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


app = create_app()
