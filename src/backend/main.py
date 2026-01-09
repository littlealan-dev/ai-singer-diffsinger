from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, AsyncIterator, Iterator, Optional
import asyncio
from contextlib import asynccontextmanager
import time
import os

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
from src.backend.job_store import JobStore, build_progress_payload
from src.backend.session import SessionStore
from src.backend.firebase_app import initialize_firebase_app, verify_id_token
from src.backend.storage_client import download_bytes, upload_file
from src.mcp.logging_utils import (
    clear_log_context,
    configure_logging,
    get_logger,
    set_log_context,
)
from firebase_admin import app_check


class ChatRequest(BaseModel):
    message: str


def create_app() -> FastAPI:
    configure_logging()
    settings = Settings.from_env()
    sessions = SessionStore(
        project_root=settings.project_root,
        sessions_dir=settings.sessions_dir,
        ttl_seconds=settings.session_ttl_seconds,
        max_sessions=settings.max_sessions,
    )
    job_store = JobStore()
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
    app.state.job_store = job_store
    app.state.router = router
    app.state.llm_client = llm_client
    app.state.orchestrator = orchestrator

    cors_env = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    if cors_env:
        cors_origins = [origin.strip() for origin in cors_env.split(",") if origin.strip()]
    else:
        cors_origins = [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:5174",
            "http://127.0.0.1:5174",
        ]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.monotonic()
        session_id = request.path_params.get("session_id") if request.path_params else None
        set_log_context(session_id=session_id)
        logger.debug(
            "http_request_start method=%s path=%s session_id=%s",
            request.method,
            request.url.path,
            session_id,
        )
        try:
            if request.method != "OPTIONS":
                await _require_app_check(request)
            response = await call_next(request)
        finally:
            duration_ms = (time.monotonic() - start) * 1000.0
            logger.debug(
                "http_request method=%s path=%s status=%s duration_ms=%.2f session_id=%s",
                request.method,
                request.url.path,
                getattr(locals().get("response"), "status_code", "error"),
                duration_ms,
                session_id,
            )
            clear_log_context()
        return response

    @app.post("/sessions")
    async def create_session(request: Request) -> Dict[str, str]:
        user_id = await _get_user_id_or_401(request)
        session = await request.app.state.sessions.create_session(user_id=user_id)
        return {"session_id": session.id}

    @app.post("/sessions/{session_id}/upload")
    async def upload_musicxml(
        session_id: str,
        request: Request,
        file: UploadFile = File(...),
    ) -> Dict[str, Any]:
        sessions: SessionStore = request.app.state.sessions
        settings: Settings = request.app.state.settings
        user_id = await _get_user_id_or_401(request)
        await _get_session_or_404(sessions, session_id, user_id)

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
        if settings.backend_use_storage:
            storage_path = _session_input_storage_path(
                user_id, session_id, target_path.suffix
            )
            content_type = file.content_type or "application/octet-stream"
            await asyncio.to_thread(
                upload_file, settings.storage_bucket, target_path, storage_path, content_type
            )
            await sessions.set_metadata(session_id, "musicxml_storage_path", storage_path)

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
        user_id = await _get_user_id_or_401(request)
        await _get_session_or_404(sessions, session_id, user_id)
        if len(payload.message) > request.app.state.settings.llm_max_message_chars:
            raise HTTPException(status_code=400, detail="Message too long.")
        try:
            return await orchestrator.handle_chat(session_id, payload.message, user_id=user_id)
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
        user_id = await _get_user_id_or_401(request)
        await _get_session_or_404(sessions, session_id, user_id)
        snapshot = None
        if file:
            snapshot = await _get_snapshot_or_404(sessions, session_id, user_id)
            current_audio = snapshot.get("current_audio") if snapshot else None
            storage_path = current_audio.get("storage_path") if current_audio else None
            if settings.backend_use_storage and storage_path:
                return await _stream_storage_audio(settings, storage_path)
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
            snapshot = await _get_snapshot_or_404(sessions, session_id, user_id)
            current_audio = snapshot.get("current_audio")
            if not current_audio:
                raise HTTPException(status_code=404, detail="No audio available for this session.")
            storage_path = current_audio.get("storage_path")
            if settings.backend_use_storage and storage_path:
                return await _stream_storage_audio(settings, storage_path)
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
        job_store: JobStore = request.app.state.job_store
        user_id = await _get_user_id_or_401(request)
        latest = await asyncio.to_thread(
            job_store.get_latest_job_by_session,
            user_id=user_id,
            session_id=session_id,
        )
        if latest is None:
            return {"status": "idle"}
        job_id, data = latest
        return build_progress_payload(job_id, data)

    @app.get("/sessions/{session_id}/score")
    async def get_score(session_id: str, request: Request) -> Response:
        sessions: SessionStore = request.app.state.sessions
        settings: Settings = request.app.state.settings
        user_id = await _get_user_id_or_401(request)
        session = await _get_session_or_404(sessions, session_id, user_id)
        rel_path = session.files.get("musicxml_path")
        if not rel_path:
            raise HTTPException(status_code=404, detail="Score not found.")
        score_path = settings.project_root / rel_path
        if not score_path.exists():
            raise HTTPException(status_code=404, detail="Score file not found.")
        content = _read_musicxml_content(score_path)
        return Response(content=content, media_type="application/xml")

    return app


async def _get_session_or_404(
    sessions: SessionStore, session_id: str, user_id: Optional[str]
) -> Any:
    try:
        return await sessions.get_session(session_id, user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Not authorized for this session.") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Session not found.") from exc


async def _get_snapshot_or_404(
    sessions: SessionStore, session_id: str, user_id: Optional[str]
) -> Dict[str, Any]:
    try:
        return await sessions.get_snapshot(session_id, user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Not authorized for this session.") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Session not found.") from exc


def _extract_bearer_token(request: Request) -> str:
    auth_header = request.headers.get("authorization")
    if not auth_header:
        token = request.query_params.get("id_token") or request.query_params.get("auth")
        if token:
            return token
        raise HTTPException(status_code=401, detail="Missing Authorization header.")
    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header.")
    return parts[1]


async def _get_user_id_or_401(request: Request) -> str:
    if request.app.state.settings.backend_auth_disabled:
        user_id = "dev-user"
        set_log_context(user_id=user_id)
        return user_id
    token = _extract_bearer_token(request)
    try:
        user_id = await asyncio.to_thread(verify_id_token, token)
        set_log_context(user_id=user_id)
        return user_id
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Firebase token.") from exc


async def _require_app_check(request: Request) -> None:
    settings: Settings = request.app.state.settings
    if not settings.backend_require_app_check:
        return
    initialize_firebase_app()
    token = request.headers.get("X-Firebase-AppCheck")
    if not token:
        token = request.query_params.get("app_check")
    if not token:
        raise HTTPException(status_code=401, detail="Missing App Check token.")
    try:
        await asyncio.to_thread(app_check.verify_token, token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid App Check token.") from exc


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


def _session_input_storage_path(user_id: str, session_id: str, suffix: str) -> str:
    safe_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    return f"sessions/{user_id}/{session_id}/input{safe_suffix}"


async def _stream_storage_audio(settings: Settings, storage_path: str) -> Response:
    suffix = Path(storage_path).suffix.lower()
    if suffix == ".wav":
        media_type = "audio/wav"
    elif suffix == ".mp3":
        media_type = "audio/mpeg"
    else:
        media_type = "application/octet-stream"
    data = await asyncio.to_thread(download_bytes, settings.storage_bucket, storage_path)
    return Response(content=data, media_type=media_type)


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
