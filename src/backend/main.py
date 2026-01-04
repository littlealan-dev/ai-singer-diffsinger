from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, AsyncIterator, Iterator, Optional
import asyncio
from contextlib import asynccontextmanager
import time

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
import logging
import zipfile
from xml.etree import ElementTree

from src.backend.config import Settings
from src.backend.llm_factory import create_llm_client
from src.backend.mcp_client import McpRouter, McpError
from src.backend.orchestrator import Orchestrator
from src.backend.progress import read_progress
from src.backend.session import SessionStore
from src.mcp.logging_utils import ensure_timestamped_handlers, get_logger


class ChatRequest(BaseModel):
    message: str


def create_app() -> FastAPI:
    ensure_timestamped_handlers()
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
            removed = await sessions.cleanup_expired_on_disk()
            if removed:
                logger = get_logger("backend.api")
                logger.info("session_cleanup_removed count=%s", removed)
            router.stop()

    app = FastAPI(title="SVS Backend", version="0.1.0", lifespan=lifespan)
    logger = get_logger("backend.api")
    logger.setLevel(logging.DEBUG)
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.router = router
    app.state.llm_client = llm_client
    app.state.orchestrator = orchestrator

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:5174",
            "http://127.0.0.1:5174",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.monotonic()
        session_id = request.path_params.get("session_id") if request.path_params else None
        logger.debug(
            "http_request_start method=%s path=%s session_id=%s",
            request.method,
            request.url.path,
            session_id,
        )
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000.0
        logger.debug(
            "http_request method=%s path=%s status=%s duration_ms=%.2f session_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            session_id,
        )
        return response

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

        score_summary = score.get("score_summary") if isinstance(score, dict) else None
        if isinstance(score, dict):
            score = dict(score)
            score.pop("score_summary", None)
        await sessions.set_score_summary(session_id, score_summary)
        version = await sessions.set_score(session_id, score)
        return {
            "session_id": session_id,
            "parsed": True,
            "current_score": {"score": score, "version": version},
            "score_summary": score_summary,
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
    async def get_audio(
        session_id: str,
        request: Request,
        file: Optional[str] = None,
        stream: bool = False,
    ) -> Response:
        sessions: SessionStore = request.app.state.sessions
        settings: Settings = request.app.state.settings
        if file:
            session_dir = sessions.session_dir(session_id)
            file_name = Path(file).name
            if file_name != file:
                raise HTTPException(status_code=400, detail="Invalid audio file name.")
            audio_path = (session_dir / file_name).resolve()
            try:
                audio_path.relative_to(session_dir)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid audio path.") from exc
        else:
            snapshot = await _get_snapshot_or_404(sessions, session_id)
            current_audio = snapshot.get("current_audio")
            if not current_audio:
                raise HTTPException(status_code=404, detail="No audio available for this session.")
            audio_path = settings.project_root / current_audio["path"]
        if not audio_path.exists():
            raise HTTPException(status_code=404, detail="Audio file not found.")
        suffix = audio_path.suffix.lower()
        if suffix == ".wav":
            media_type = "audio/wav"
        elif suffix == ".mp3":
            media_type = "audio/mpeg"
        else:
            media_type = "application/octet-stream"
        if stream:
            headers = {"Content-Length": str(audio_path.stat().st_size)}
            return StreamingResponse(
                _iter_file(audio_path),
                media_type=media_type,
                headers=headers,
            )
        return FileResponse(audio_path, media_type=media_type, filename=audio_path.name)

    @app.get("/sessions/{session_id}/progress")
    async def get_progress(session_id: str, request: Request) -> Dict[str, Any]:
        sessions: SessionStore = request.app.state.sessions
        await _get_session_or_404(sessions, session_id)
        progress_path = sessions.progress_path(session_id)
        payload = read_progress(progress_path)
        if payload is None:
            return {"status": "idle"}
        return payload

    @app.get("/sessions/{session_id}/score")
    async def get_score(session_id: str, request: Request) -> Response:
        sessions: SessionStore = request.app.state.sessions
        settings: Settings = request.app.state.settings
        session = await _get_session_or_404(sessions, session_id)
        rel_path = session.files.get("musicxml_path")
        if not rel_path:
            raise HTTPException(status_code=404, detail="Score not found.")
        score_path = settings.project_root / rel_path
        if not score_path.exists():
            raise HTTPException(status_code=404, detail="Score file not found.")
        content = _read_musicxml_content(score_path)
        return Response(content=content, media_type="application/xml")

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


def _read_musicxml_content(path: Path) -> str:
    if path.suffix.lower() != ".mxl":
        return path.read_text(encoding="utf-8", errors="replace")
    with zipfile.ZipFile(path) as archive:
        xml_name = _find_mxl_xml(archive)
        xml_bytes = archive.read(xml_name)
    return xml_bytes.decode("utf-8", errors="replace")


def _iter_file(path: Path, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            yield chunk


def _find_mxl_xml(archive: zipfile.ZipFile) -> str:
    try:
        container_bytes = archive.read("META-INF/container.xml")
    except KeyError:
        return _first_mxl_xml(archive)
    try:
        root = ElementTree.fromstring(container_bytes)
    except ElementTree.ParseError:
        return _first_mxl_xml(archive)
    for elem in root.iter():
        if elem.tag.endswith("rootfile"):
            full_path = elem.attrib.get("full-path")
            if full_path and full_path in archive.namelist():
                return full_path
    return _first_mxl_xml(archive)


def _first_mxl_xml(archive: zipfile.ZipFile) -> str:
    candidates = [
        name
        for name in archive.namelist()
        if name.lower().endswith(".xml") and not name.startswith("META-INF/")
    ]
    if not candidates:
        raise HTTPException(status_code=400, detail="No MusicXML file found in archive.")
    return candidates[0]


app = create_app()
