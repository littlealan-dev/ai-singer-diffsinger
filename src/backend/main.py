from __future__ import annotations

"""FastAPI entrypoint for the backend service."""

from pathlib import Path
from typing import Any, Dict, AsyncIterator, Iterator, Literal, Optional
import asyncio
from contextlib import asynccontextmanager
import time
import os
import shutil
import tempfile
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, EmailStr, Field
import logging

from src.backend.config import Settings
from src.backend.credit_retry import retry_credit_op
from src.backend.llm_factory import create_llm_client
from src.backend.mcp_client import (
    McpRouter,
    McpError,
    McpRequestTimeoutError,
    McpStartupInProgressError,
    McpToolError,
)
from src.backend.orchestrator import Orchestrator
from src.backend.audio_mix import MixTrackSource, get_audio_duration_seconds, render_mix_to_wav
from src.backend.job_store import JobStore, build_progress_payload
from src.backend.message_catalog import backend_message
from src.backend.session import SessionStore, FirestoreSessionStore
from src.backend.firebase_app import (
    get_firestore_client,
    initialize_firebase_app,
    verify_id_token_claims,
)
from src.backend.storage_client import download_bytes, upload_file
from src.backend.waitlist import subscribe_to_waitlist, verify_app_check_token
from src.backend.turnstile import verify_turnstile_token
from src.backend.playback_tokens import (
    PlaybackTokenClaims,
    PlaybackTokenError,
    issue_playback_token,
    verify_playback_token,
)
from src.backend.secret_manager import read_secret
from src.musicxml.io import (
    MusicXmlArchiveError,
    MusicXmlArchiveTooLargeError,
    read_musicxml_content as read_musicxml_content_bounded,
)
from src.mcp.logging_utils import (
    clear_log_context,
    configure_logging,
    get_logger,
    set_log_context,
)
from firebase_admin import app_check

_PLAYBACK_SECRET_CACHE: dict[tuple[str | None, str, str], str] = {}


def _default_solfege_settings_response() -> Dict[str, Any]:
    return {"system": "movable_do", "mode": "major", "revision": 1}


class ChatRequest(BaseModel):
    """Request payload for chat-based interactions."""
    message: str
    # Optional structured selector payload from UI widgets (for example verse dropdown).
    # Values are treated as authoritative user selections and avoid fragile text parsing.
    selection: dict[str, Any] | None = None
    selected_voicebank_id: str | None = None
    # Backend-ready structured override. UI controls will be added separately.
    selected_language: str | None = Field(
        default=None,
        pattern=r"^[a-z]{2,3}(?:-[a-z0-9]+)*$",
    )


class SolfegeSettingsPayload(BaseModel):
    """Canonical user-selectable solfege settings."""
    system: Literal["movable_do", "fixed_do"]
    mode: Literal["major", "minor_la_based", "minor_do_based"]


class SolfegeSettingsRequest(BaseModel):
    """Direct UI request to apply confirmed solfege settings."""
    settings: SolfegeSettingsPayload


class WaitlistSubscribeRequest(BaseModel):
    """Request payload for waitlist subscriptions."""
    email: EmailStr
    first_name: str | None = None
    feedback: str | None = None
    gdpr_consent: bool
    consent_text: str
    source: str


class MarketingOptInRequest(BaseModel):
    """Request payload for authenticated marketing email opt-in."""
    consent_text: str
    source: str
    pending_intent_created_at: str | None = None


class BillingCheckoutRequest(BaseModel):
    planKey: str


class TopupCheckoutRequest(BaseModel):
    packKey: str = "topup_15"


class EmbeddedCheckoutRequest(BaseModel):
    checkoutType: str
    planKey: str | None = None
    packKey: str | None = None


class BillingCheckoutSyncRequest(BaseModel):
    sessionId: str


class ExportMixTrackRequest(BaseModel):
    job_id: str
    part_id: str
    key: str | None = None
    label: str | None = None
    verse_number: str | int | None = None
    muted: bool = False
    solo: bool = False
    volume: float = 1.0


class ExportMixRequest(BaseModel):
    tracks: list[ExportMixTrackRequest]
    billing_reference_job_id: str
    format: Literal["wav"] = "wav"


class MaintenanceStatusResponse(BaseModel):
    enabled: bool
    allowed: bool
    message: str | None = None


class TurnstileVerifyRequest(BaseModel):
    token: str


class TurnstileVerifyResponse(BaseModel):
    success: bool


class FeedbackPromptedRequest(BaseModel):
    jobId: str
    trigger: str = "unknown"


class FeedbackSubmitRequest(BaseModel):
    jobId: str
    ratings: dict[str, Any]
    comment: Any = ""
    client: dict[str, Any] | None = None


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    configure_logging()
    settings = Settings.from_env()
    if settings.app_env.lower() in {"dev", "development", "local", "test"}:
        # Use filesystem-backed sessions in development.
        sessions = SessionStore(
            project_root=settings.project_root,
            sessions_dir=settings.sessions_dir,
            ttl_seconds=settings.session_ttl_seconds,
            max_sessions=settings.max_sessions,
            backend_use_storage=settings.backend_use_storage,
            storage_bucket=settings.storage_bucket,
        )
    else:
        # Use Firestore-backed sessions in production.
        sessions = FirestoreSessionStore(
            project_root=settings.project_root,
            sessions_dir=settings.sessions_dir,
            ttl_seconds=settings.session_ttl_seconds,
            max_sessions=settings.max_sessions,
            backend_use_storage=settings.backend_use_storage,
            storage_bucket=settings.storage_bucket,
        )
    job_store = JobStore()
    router = McpRouter(settings)
    llm_client = create_llm_client(settings)
    orchestrator = Orchestrator(router, sessions, settings, llm_client)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Start/stop shared services and handle cleanup."""
        settings.sessions_dir.mkdir(parents=True, exist_ok=True)
        if settings.mcp_startup_blocking:
            router.start()
            _log_onnx_providers()
        else:
            logger.info("mcp_start_deferred")
            router.start_background()
            asyncio.create_task(asyncio.to_thread(_log_onnx_providers))
        try:
            yield
        finally:
            removed = await sessions.cleanup_expired_on_disk()
            if removed:
                get_logger("backend.api").info("session_cleanup_removed count=%s", removed)
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
    app.state.export_mix_tasks = {}

    cors_env = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    if cors_env:
        cors_origins = [origin.strip() for origin in cors_env.split(",") if origin.strip()]
    else:
        # Default local dev origins.
        cors_origins = [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:5174",
            "http://127.0.0.1:5174",
            "http://localhost:5002",
            "http://127.0.0.1:5002",
            "http://localhost:5003",
            "http://127.0.0.1:5003",
        ]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz() -> Dict[str, str]:
        """Return process liveness without depending on MCP worker readiness."""
        return {"status": "ok", "build": settings.backend_build_id}

    @app.get("/readyz")
    async def readyz(request: Request) -> Dict[str, Any]:
        """Return app readiness diagnostics without requiring MCP tool calls."""
        router: McpRouter = request.app.state.router
        mcp = router.readiness()
        return {
            "status": "ready" if mcp["ready"] else "starting",
            "ready": bool(mcp["ready"]),
            "build": settings.backend_build_id,
            "mcp": mcp,
        }

    @app.post("/auth/turnstile/verify", response_model=TurnstileVerifyResponse)
    async def verify_turnstile(request: Request, body: TurnstileVerifyRequest) -> TurnstileVerifyResponse:
        """Verify a Cloudflare Turnstile token before sign-in actions."""
        token = body.token.strip()
        if not token:
            raise HTTPException(status_code=400, detail="Missing Turnstile token.")
        result = await verify_turnstile_token(
            settings,
            token=token,
            remote_ip=_client_ip(request),
        )
        if not result.success:
            logger.warning("turnstile_verification_failed errors=%s", result.error_codes)
            raise HTTPException(status_code=403, detail="Human verification failed.")
        return TurnstileVerifyResponse(success=True)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        """Attach per-request logging context and timings."""
        start = time.monotonic()
        session_id = request.path_params.get("session_id") if request.path_params else None
        response = None
        set_log_context(session_id=session_id)
        logger.debug(
            "http_request_start method=%s path=%s session_id=%s",
            request.method,
            request.url.path,
            session_id,
        )
        try:
            if request.method != "OPTIONS" and _should_require_app_check(request):
                try:
                    await _require_app_check(request)
                except HTTPException as exc:
                    response = JSONResponse(
                        status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=exc.headers,
                    )
                    return response
            response = await call_next(request)
        finally:
            duration_ms = (time.monotonic() - start) * 1000.0
            logger.debug(
                "http_request method=%s path=%s status=%s duration_ms=%.2f session_id=%s",
                request.method,
                request.url.path,
                getattr(response, "status_code", "error"),
                duration_ms,
                session_id,
            )
            clear_log_context()
        return response

    @app.post("/sessions")
    async def create_session(request: Request) -> Dict[str, str]:
        """Create a new session for a user."""
        user_id = await _get_user_id_or_401(request)
        session = await request.app.state.sessions.create_session(user_id=user_id)
        return {"session_id": session.id}

    @app.post("/sessions/{session_id}/upload")
    async def upload_musicxml(
        session_id: str,
        request: Request,
        file: UploadFile = File(...),
    ) -> Dict[str, Any]:
        """Upload a MusicXML file, parse it, and attach to a session."""
        sessions: SessionStore = request.app.state.sessions
        settings: Settings = request.app.state.settings
        job_store: JobStore = request.app.state.job_store
        user_id, user_email = await _get_user_context_or_401(request)
        await _require_active_credits(user_id, user_email)
        await _get_session_or_404(sessions, session_id, user_id)

        original_name = Path(file.filename or "").name
        suffix = Path(original_name).suffix.lower()
        if suffix not in {".xml", ".mxl"}:
            raise HTTPException(status_code=400, detail="Only .xml or .mxl files are supported.")

        temp_dir = Path(tempfile.mkdtemp(prefix="upload-", dir=settings.data_dir))
        temp_upload_path = temp_dir / f"score{suffix}"
        temp_canonical_path = temp_upload_path
        try:
            upload_write_start = time.monotonic()
            uploaded_bytes = await _write_upload(temp_upload_path, file, settings.max_upload_bytes)
            upload_write_ms = (time.monotonic() - upload_write_start) * 1000.0
            normalize_mxl_ms = 0.0
            if suffix == ".mxl":
                temp_canonical_path = temp_dir / "score.xml"
                normalize_start = time.monotonic()
                await asyncio.to_thread(
                    _normalize_uploaded_mxl,
                    temp_upload_path,
                    temp_canonical_path,
                    max_mxl_uncompressed_bytes=settings.max_mxl_uncompressed_bytes,
                )
                normalize_mxl_ms = (time.monotonic() - normalize_start) * 1000.0

            rel_path = str(temp_canonical_path.relative_to(settings.project_root))
            try:
                parse_score_start = time.monotonic()
                score = await asyncio.to_thread(
                    request.app.state.router.call_tool,
                    "parse_score",
                    {"file_path": rel_path, "expand_repeats": False},
                )
                parse_score_ms = (time.monotonic() - parse_score_start) * 1000.0
            except McpStartupInProgressError as exc:
                raise _backend_starting_http_exception(exc) from exc
            except McpRequestTimeoutError as exc:
                parse_score_ms = (time.monotonic() - parse_score_start) * 1000.0
                logger.warning(
                    "upload_parse_timeout session_id=%s user_id=%s suffix=%s "
                    "uploaded_bytes=%s parse_score_ms=%.2f timeout_seconds=%.2f retry_skipped=true",
                    session_id,
                    user_id,
                    suffix,
                    uploaded_bytes,
                    parse_score_ms,
                    exc.timeout_seconds,
                )
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "score_parse_timeout",
                        "message": (
                            "This score took too long to parse. Try a simpler "
                            "MusicXML export or contact support."
                        ),
                    },
                ) from exc
            except McpToolError as exc:
                if exc.code == "invalid_musicxml":
                    logger.warning(
                        "upload_invalid_musicxml session_id=%s user_id=%s suffix=%s "
                        "uploaded_bytes=%s retry_skipped=true",
                        session_id,
                        user_id,
                        suffix,
                        uploaded_bytes,
                    )
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "code": "invalid_musicxml",
                            "message": str(exc),
                        },
                    ) from exc
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            except McpError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            logger.info(
                "upload_musicxml_timing session_id=%s suffix=%s upload_write_ms=%.2f "
                "normalize_mxl_ms=%.2f parse_score_ms=%.2f uploaded_bytes=%s",
                session_id,
                suffix,
                upload_write_ms,
                normalize_mxl_ms,
                parse_score_ms,
                uploaded_bytes,
            )

            await sessions.reset_for_new_upload(session_id)
            await asyncio.to_thread(
                job_store.clear_jobs_for_session,
                user_id=user_id,
                session_id=session_id,
            )

            session_dir = sessions.session_dir(session_id)
            session_dir.mkdir(parents=True, exist_ok=True)
            target_path = session_dir / f"score{suffix}"
            temp_upload_path.replace(target_path)
            canonical_musicxml_path = target_path
            if suffix == ".mxl":
                canonical_musicxml_path = session_dir / "score.xml"
                temp_canonical_path.replace(canonical_musicxml_path)

            await sessions.set_file(session_id, "musicxml_path", canonical_musicxml_path)
            await sessions.set_file(session_id, "uploaded_musicxml_path", target_path)
            if original_name:
                await sessions.set_metadata(session_id, "musicxml_name", original_name)
            if settings.backend_use_storage:
                # Persist the uploaded file in object storage when configured.
                storage_path = _session_input_storage_path(
                    user_id, session_id, target_path.suffix
                )
                content_type = file.content_type or "application/octet-stream"
                await asyncio.to_thread(
                    upload_file, settings.storage_bucket, target_path, storage_path, content_type
                )
                await sessions.set_metadata(session_id, "musicxml_storage_path", storage_path)
            if isinstance(score, dict):
                score = dict(score)
                score["source_musicxml_path"] = str(canonical_musicxml_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        score_summary = score.get("score_summary") if isinstance(score, dict) else None
        if isinstance(score, dict):
            score = dict(score)
            score.pop("score_summary", None)
        await sessions.set_score_summary(session_id, score_summary)
        await sessions.set_original_score(session_id, score)
        version = await sessions.set_score(session_id, score)
        return {
            "session_id": session_id,
            "parsed": True,
            "current_score": {"score": score, "version": version},
            "score_summary": score_summary,
            "solfege_settings": _default_solfege_settings_response(),
        }

    @app.post("/sessions/{session_id}/chat")
    async def chat(session_id: str, request: Request, payload: ChatRequest) -> Dict[str, Any]:
        """Handle chat requests and orchestrate LLM/tool execution."""
        sessions: SessionStore = request.app.state.sessions
        orchestrator: Orchestrator = request.app.state.orchestrator
        user_id, user_email = await _get_user_context_or_401(request)
        await _require_active_credits(user_id, user_email)
        await _get_session_or_404(sessions, session_id, user_id)
        if len(payload.message) > request.app.state.settings.llm_max_message_chars:
            raise HTTPException(status_code=400, detail="Message too long.")
        try:
            response = await orchestrator.handle_chat(
                session_id,
                payload.message,
                user_id=user_id,
                user_email=user_email,
                selection=payload.selection,
                selected_voicebank_id=payload.selected_voicebank_id,
                selected_language=payload.selected_language,
            )
            return _sign_audio_payload_urls(request, response, user_id=user_id)
        except McpStartupInProgressError as exc:
            raise _backend_starting_http_exception(exc) from exc
        except McpError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/sessions/{session_id}/solfege-settings")
    async def get_solfege_settings(session_id: str, request: Request) -> Dict[str, Any]:
        """Return canonical solfege settings for the active score session."""
        sessions: SessionStore = request.app.state.sessions
        orchestrator: Orchestrator = request.app.state.orchestrator
        user_id = await _get_user_id_or_401(request)
        await _get_session_or_404(sessions, session_id, user_id)
        return await orchestrator.get_solfege_settings(session_id, user_id=user_id)

    @app.patch("/sessions/{session_id}/solfege-settings")
    async def patch_solfege_settings(
        session_id: str,
        request: Request,
        payload: SolfegeSettingsRequest,
    ) -> Dict[str, Any]:
        """Apply confirmed UI settings and rewrite all generated solfege verses."""
        sessions: SessionStore = request.app.state.sessions
        orchestrator: Orchestrator = request.app.state.orchestrator
        user_id = await _get_user_id_or_401(request)
        await _get_session_or_404(sessions, session_id, user_id)
        try:
            return await orchestrator.update_solfege_settings(
                session_id,
                user_id=user_id,
                system=payload.settings.system,
                mode=payload.settings.mode,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/voicebanks")
    async def list_available_voicebanks(request: Request) -> Dict[str, Any]:
        """Return enabled voicebanks from the active manifest for UI selectors."""
        await _get_user_context_or_401(request)
        from src.api.voicebank_cache import get_enabled_manifest_voicebanks

        entries = get_enabled_manifest_voicebanks()
        voicebanks = []
        for entry in entries:
            voicebank_id = entry.get("id")
            if not isinstance(voicebank_id, str) or not voicebank_id:
                continue
            voicebanks.append(
                {
                    "id": voicebank_id,
                    "name": entry.get("name") or voicebank_id,
                    "gender": entry.get("gender"),
                    "voice_type": entry.get("voice_type"),
                    "default_voice_color": entry.get("default_voice_color"),
                    "profile_image": entry.get("profile_image"),
                    "selector_image": entry.get("selector_image"),
                }
            )
        return {"voicebanks": voicebanks}

    @app.get("/sessions/{session_id}/audio")
    async def get_audio(
        session_id: str,
        request: Request,
        file: Optional[str] = None,
        stream: bool = False,
    ) -> Response:
        sessions: SessionStore = request.app.state.sessions
        settings: Settings = request.app.state.settings
        claims = _get_playback_claims_or_401(request, settings, session_id, file)
        user_id = claims.user_id
        await _get_session_or_404(sessions, session_id, user_id)
        snapshot = None
        if file:
            snapshot = await _get_snapshot_or_404(sessions, session_id, user_id)
            current_audio = snapshot.get("current_audio") if snapshot else None
            session_dir = sessions.session_dir(session_id)
            file_name = Path(file).name
            if file_name != file:
                raise HTTPException(status_code=400, detail="Invalid audio file name.")
            storage_path = claims.resource_path
            if settings.backend_use_storage and storage_path:
                return await _stream_storage_audio(
                    request,
                    settings,
                    storage_path,
                    download=bool(request.query_params.get("download")),
                    file_name=file_name,
                )
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
                current_file_name = Path(current_audio["path"]).name
                return await _stream_storage_audio(
                    request,
                    settings,
                    storage_path,
                    download=bool(request.query_params.get("download")),
                    file_name=current_file_name,
                )
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
        requested_job_id = str(request.query_params.get("job_id") or "").strip()
        if requested_job_id:
            job = await asyncio.to_thread(
                job_store.get_job_by_id,
                job_id=requested_job_id,
                user_id=user_id,
                session_id=session_id,
            )
        else:
            job = await asyncio.to_thread(
                job_store.get_latest_job_by_session,
                user_id=user_id,
                session_id=session_id,
            )
        if job is None:
            return {"status": "idle"}
        job_id, data = job
        payload = build_progress_payload(job_id, data)
        return _sign_audio_payload_urls(
            request,
            payload,
            user_id=user_id,
            resource_path=data.get("outputPath") if isinstance(data.get("outputPath"), str) else None,
        )

    @app.post("/sessions/{session_id}/export-mix")
    async def export_mix(
        session_id: str,
        request: Request,
        payload: ExportMixRequest,
    ) -> Dict[str, Any]:
        """Start an async mixdown job for the current multitrack state."""
        sessions: SessionStore = request.app.state.sessions
        settings: Settings = request.app.state.settings
        job_store: JobStore = request.app.state.job_store
        user_id, user_email = await _get_user_context_or_401(request)
        await _get_session_or_404(sessions, session_id, user_id)
        selected_tracks = _select_export_mix_tracks(payload.tracks)
        job_id = uuid.uuid4().hex
        billing_context = await _build_export_mix_billing_context(
            settings=settings,
            sessions=sessions,
            job_store=job_store,
            session_id=session_id,
            user_id=user_id,
            all_tracks=payload.tracks,
            selected_tracks=selected_tracks,
            billing_reference_job_id=payload.billing_reference_job_id,
        )
        from src.backend.credits import get_or_create_credits, reserve_credits

        user_credits = await asyncio.to_thread(get_or_create_credits, user_id, user_email)
        reserve_result = await retry_credit_op(
            reserve_credits,
            user_id,
            job_id,
            billing_context["required_credits"],
            settings.session_ttl_seconds,
            session_id=session_id,
            job_kind="export_mix",
            pricing="export_mix_v1",
            pricing_unit_seconds=60,
            billable_duration_seconds=billing_context["billable_duration_seconds"],
            billing_reference_job_id=billing_context["billing_reference_job_id"],
            max_attempts=settings.credit_retry_max_attempts,
            base_delay=settings.credit_retry_base_delay_seconds,
        )
        if reserve_result.status in {"insufficient_balance", "overdrafted"}:
            raise HTTPException(
                status_code=402,
                detail={
                    "type": "insufficient_credits",
                    "job_kind": "export_mix",
                    "message": backend_message(
                        "account.insufficient_credits",
                        estimated_credits=billing_context["required_credits"],
                        available_credits=user_credits.available_balance,
                    ),
                    "required_credits": billing_context["required_credits"],
                    "available_credits": user_credits.available_balance,
                },
            )
        if reserve_result.status == "expired":
            raise HTTPException(status_code=403, detail=backend_message("account.free_trial_expired"))
        if reserve_result.status not in {"reserved", "reservation_exists"}:
            raise HTTPException(status_code=503, detail=backend_message("billing.setup_failed_retry"))
        try:
            await asyncio.to_thread(
                job_store.create_job,
                job_id=job_id,
                user_id=user_id,
                session_id=session_id,
                status="queued",
                render_type="export_mix",
            )
            await asyncio.to_thread(
                job_store.update_job,
                job_id,
                status="queued",
                step="export_mix",
                progress=0.0,
                jobKind="export_mix",
                billing={
                    "pricing": "export_mix_v1",
                    "pricingUnitSeconds": 60,
                    "billingReferenceJobId": billing_context["billing_reference_job_id"],
                    "billableDurationSeconds": billing_context["billable_duration_seconds"],
                    "requiredCredits": billing_context["required_credits"],
                    "reservationStatus": "pending",
                },
                mix={
                    "format": payload.format,
                    "trackCount": len(selected_tracks),
                    "tracks": [_export_mix_track_metadata(track) for track in selected_tracks],
                },
            )
        except Exception as exc:
            await _release_export_mix_reservation(
                settings=settings,
                job_store=job_store,
                user_id=user_id,
                job_id=job_id,
                error_message=f"Export mix job startup failed: {exc}",
            )
            raise
        task = asyncio.create_task(
            _run_export_mix_job(
                settings=settings,
                sessions=sessions,
                job_store=job_store,
                session_id=session_id,
                user_id=user_id,
                job_id=job_id,
                tracks=selected_tracks,
                billable_duration_seconds=billing_context["billable_duration_seconds"],
            )
        )
        request.app.state.export_mix_tasks[job_id] = task

        def _cleanup(_: asyncio.Task) -> None:
            request.app.state.export_mix_tasks.pop(job_id, None)

        task.add_done_callback(_cleanup)
        logger.info(
            "mix_export_job_created session_id=%s user_id=%s job_id=%s tracks=%s",
            session_id,
            user_id,
            job_id,
            len(selected_tracks),
        )
        return {
            "status": "queued",
            "progress_url": f"/sessions/{session_id}/progress?job_id={job_id}",
            "job_id": job_id,
            "required_credits": billing_context["required_credits"],
            "billable_duration_seconds": billing_context["billable_duration_seconds"],
        }

    @app.post("/feedback/prompted")
    async def mark_audio_feedback_prompted(
        request: Request,
        payload: FeedbackPromptedRequest,
    ) -> Dict[str, str]:
        """Record that a feedback prompt was displayed for a completed audio job."""
        user_id = await _get_user_id_or_401(request)
        from src.backend.feedback import FeedbackError, mark_feedback_prompted

        try:
            return await asyncio.to_thread(
                mark_feedback_prompted,
                uid=user_id,
                job_id=payload.jobId,
                trigger=payload.trigger,
            )
        except FeedbackError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.post("/feedback")
    async def submit_audio_feedback(
        request: Request,
        payload: FeedbackSubmitRequest,
    ) -> Dict[str, str]:
        """Submit one idempotent feedback form for a completed audio job."""
        user_id = await _get_user_id_or_401(request)
        from src.backend.feedback import FeedbackError, submit_audio_feedback as persist_audio_feedback

        try:
            return await asyncio.to_thread(
                persist_audio_feedback,
                uid=user_id,
                job_id=payload.jobId,
                ratings=payload.ratings,
                comment=payload.comment,
                client=payload.client,
            )
        except FeedbackError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.get("/credits")
    async def get_credits(request: Request) -> Dict[str, Any]:
        """Fetch user credit balance and expiry."""
        user_id, user_email = await _get_user_context_or_401(request)
        from src.backend.credits import get_or_create_credits
        user_credits = await asyncio.to_thread(get_or_create_credits, user_id, user_email)
        return {
            "balance": user_credits.balance,
            "reserved": user_credits.reserved,
            "available": user_credits.available_balance,
            "expires_at": user_credits.expires_at.isoformat() if user_credits.expires_at else None,
            "overdrafted": user_credits.overdrafted,
            "is_expired": user_credits.is_expired,
            "monthly_allowance": user_credits.monthly_allowance,
            "last_grant_type": user_credits.last_grant_type,
            "last_grant_at": (
                user_credits.last_grant_at.isoformat() if user_credits.last_grant_at else None
            ),
        }

    @app.get("/maintenance/status")
    async def get_maintenance_status(request: Request) -> MaintenanceStatusResponse:
        """Return maintenance status for the authenticated user without exposing allowlists."""
        user_id, user_email = await _get_user_context_without_maintenance_or_401(
            request,
            prefer_token_when_auth_disabled=True,
        )
        config = await asyncio.to_thread(_get_maintenance_config)
        enabled, allowed, message = _evaluate_maintenance_access(config, user_id, user_email)
        return MaintenanceStatusResponse(enabled=enabled, allowed=allowed, message=message)

    @app.post("/billing/checkout-session")
    async def create_billing_checkout_session(
        body: BillingCheckoutRequest,
        request: Request,
    ) -> Dict[str, str]:
        user_id, user_email = await _get_user_context_or_401(request)
        from src.backend.billing_checkout import create_checkout_session
        from src.backend.billing_types import BillingHttpError

        try:
            url = await asyncio.to_thread(
                create_checkout_session,
                user_id,
                user_email,
                body.planKey,
            )
        except BillingHttpError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return {"url": url}

    @app.post("/billing/topup-checkout-session")
    async def create_billing_topup_checkout_session(
        body: TopupCheckoutRequest,
        request: Request,
    ) -> Dict[str, Any]:
        user_id, user_email = await _get_user_context_or_401(request)
        from src.backend.billing_topup import create_topup_checkout_session
        from src.backend.billing_types import BillingHttpError

        try:
            return await asyncio.to_thread(
                create_topup_checkout_session,
                user_id,
                user_email,
                body.packKey,
            )
        except BillingHttpError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.post("/billing/embedded-checkout-session")
    async def create_billing_embedded_checkout_session(
        body: EmbeddedCheckoutRequest,
        request: Request,
    ) -> Dict[str, Any]:
        user_id, user_email = await _get_user_context_or_401(request)
        from src.backend.billing_embedded_checkout import create_embedded_checkout_session
        from src.backend.billing_types import BillingHttpError

        try:
            return await asyncio.to_thread(
                create_embedded_checkout_session,
                user_id,
                user_email,
                body.checkoutType,
                plan_key=body.planKey,
                pack_key=body.packKey,
            )
        except BillingHttpError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.post("/billing/checkout-session/sync")
    async def sync_billing_checkout_session(
        body: BillingCheckoutSyncRequest,
        request: Request,
    ) -> Dict[str, Any]:
        user_id, _ = await _get_user_context_or_401(request)
        from src.backend.billing_checkout_sync import sync_checkout_session
        from src.backend.billing_types import BillingHttpError

        try:
            return await asyncio.to_thread(sync_checkout_session, user_id, body.sessionId)
        except BillingHttpError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.post("/billing/topup-checkout-session/sync")
    async def sync_billing_topup_checkout_session(
        body: BillingCheckoutSyncRequest,
        request: Request,
    ) -> Dict[str, Any]:
        user_id, _ = await _get_user_context_or_401(request)
        from src.backend.billing_topup import sync_topup_checkout_session
        from src.backend.billing_types import BillingHttpError

        try:
            return await asyncio.to_thread(sync_topup_checkout_session, user_id, body.sessionId)
        except BillingHttpError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.post("/billing/topup-checkout-session/cancel")
    async def cancel_billing_topup_checkout_session(
        body: BillingCheckoutSyncRequest,
        request: Request,
    ) -> Dict[str, Any]:
        user_id, _ = await _get_user_context_or_401(request)
        from src.backend.billing_topup import cancel_topup_checkout_session
        from src.backend.billing_types import BillingHttpError

        try:
            return await asyncio.to_thread(cancel_topup_checkout_session, user_id, body.sessionId)
        except BillingHttpError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.post("/billing/portal-session")
    async def create_billing_portal_session(request: Request) -> Dict[str, str]:
        user_id, _ = await _get_user_context_or_401(request)
        from src.backend.billing_portal import create_portal_session
        from src.backend.billing_types import BillingHttpError

        try:
            url = await asyncio.to_thread(create_portal_session, user_id)
        except BillingHttpError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return {"url": url}

    @app.post("/billing/subscription/sync")
    async def sync_billing_subscription(request: Request) -> Dict[str, Any]:
        user_id, _ = await _get_user_context_or_401(request)
        from src.backend.billing_subscription_sync import sync_current_subscription
        from src.backend.billing_types import BillingHttpError

        try:
            return await asyncio.to_thread(sync_current_subscription, user_id)
        except BillingHttpError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.post("/billing/webhook")
    async def stripe_billing_webhook(request: Request) -> Dict[str, str]:
        from src.backend.billing_types import BillingHttpError
        from src.backend.billing_webhooks import construct_stripe_event, handle_event

        signature = request.headers.get("Stripe-Signature")
        if not signature:
            raise HTTPException(status_code=400, detail="Missing Stripe-Signature header.")
        payload = await request.body()
        try:
            event = await asyncio.to_thread(construct_stripe_event, payload, signature)
            await asyncio.to_thread(handle_event, event)
        except BillingHttpError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        except Exception as exc:
            logger.exception("stripe_webhook_failed")
            raise HTTPException(status_code=400, detail="Invalid Stripe webhook.") from exc
        return {"status": "ok"}

    @app.post("/waitlist/subscribe")
    async def waitlist_subscribe(
        request_body: WaitlistSubscribeRequest,
        request: Request,
    ) -> Dict[str, Any]:
        """Subscribe a user to the waiting list."""
        settings: Settings = request.app.state.settings
        app_check_token = request.headers.get("X-Firebase-AppCheck")
        if settings.backend_require_app_check:
            if not app_check_token:
                raise HTTPException(status_code=401, detail="Missing App Check token.")
            if not verify_app_check_token(app_check_token):
                raise HTTPException(status_code=403, detail="App Check verification failed.")
        if not request_body.gdpr_consent:
            raise HTTPException(status_code=400, detail="GDPR consent is required.")
        result = await subscribe_to_waitlist(
            settings,
            email=request_body.email,
            first_name=request_body.first_name,
            feedback=request_body.feedback,
            gdpr_consent=request_body.gdpr_consent,
            consent_text=request_body.consent_text,
            source=request_body.source,
        )
        if not result.success:
            raise HTTPException(status_code=result.status_code, detail=result.message)
        authenticated_context = await _get_optional_verified_user_context(request)
        if authenticated_context is not None:
            user_id, user_email = authenticated_context
            if user_email.strip().lower() == str(request_body.email).strip().lower():
                from src.backend.marketing_opt_in import mark_marketing_opt_in_requested

                await asyncio.to_thread(
                    mark_marketing_opt_in_requested,
                    uid=user_id,
                    email=user_email,
                    source=f"{request_body.source}_authenticated",
                    consent_text=request_body.consent_text,
                    brevo_status="doi_requested",
                )
        return {
            "success": result.success,
            "message": result.message,
            "requires_confirmation": result.requires_confirmation,
        }

    @app.post("/marketing/opt-in")
    async def marketing_opt_in(
        request_body: MarketingOptInRequest,
        request: Request,
    ) -> Dict[str, Any]:
        """Request marketing email opt-in for an authenticated user."""
        if not request_body.consent_text.strip():
            raise HTTPException(status_code=400, detail="Consent text is required.")
        if not request_body.source.strip():
            raise HTTPException(status_code=400, detail="Source is required.")

        user_id, user_email = await _get_user_context_or_401(request)
        if not user_email:
            raise HTTPException(status_code=400, detail="Authenticated email is required.")
        from src.backend.marketing_opt_in import request_authenticated_marketing_opt_in

        result = await request_authenticated_marketing_opt_in(
            request.app.state.settings,
            uid=user_id,
            email=user_email,
            source=request_body.source,
            consent_text=request_body.consent_text,
        )
        if not result.success:
            raise HTTPException(status_code=result.status_code, detail=result.message)
        return {
            "success": result.success,
            "status": result.status,
            "message": result.message,
            "requires_confirmation": result.requires_confirmation,
        }

    @app.get("/sessions/{session_id}/score")
    async def get_score(session_id: str, request: Request) -> Response:
        sessions: SessionStore = request.app.state.sessions
        settings: Settings = request.app.state.settings
        user_id = await _get_user_id_or_401(request)
        snapshot = await _get_snapshot_or_404(sessions, session_id, user_id)
        score_path = _resolve_session_score_path(settings, snapshot.get("current_score"))
        if score_path is None:
            session = await _get_session_or_404(sessions, session_id, user_id)
            rel_path = session.files.get("musicxml_path")
            if not rel_path:
                raise HTTPException(status_code=404, detail="Score not found.")
            score_path = _resolve_allowlisted_score_path(settings, rel_path)
        if not score_path.exists():
            raise HTTPException(status_code=404, detail="Score file not found.")
        content = _read_musicxml_content(
            score_path,
            max_mxl_uncompressed_bytes=settings.max_mxl_uncompressed_bytes,
        )
        # A session keeps one stable score URL while transforms replace the
        # underlying MusicXML.  Prevent browsers and intermediaries from
        # serving the score that was current before the latest transform.
        return Response(
            content=content,
            media_type="application/xml",
            headers={"Cache-Control": "no-store"},
        )

    return app


def _log_onnx_providers() -> None:
    """Log ONNX Runtime available providers at startup."""
    logger = get_logger("backend.ort")
    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        logger.info("onnxruntime_providers=%s", providers)
    except Exception as exc:
        logger.warning("onnxruntime_providers_error=%s", exc)


def _select_export_mix_tracks(
    tracks: list[ExportMixTrackRequest],
) -> list[ExportMixTrackRequest]:
    if not tracks:
        raise HTTPException(status_code=400, detail="No tracks were provided for export.")
    has_solo = any(track.solo for track in tracks)
    selected = [track for track in tracks if track.solo] if has_solo else [
        track for track in tracks if not track.muted
    ]
    if not selected:
        raise HTTPException(status_code=400, detail="No audible tracks are selected for export.")
    for track in selected:
        if not track.job_id.strip():
            raise HTTPException(status_code=400, detail="Every exported track must include a job_id.")
        if not track.part_id.strip():
            raise HTTPException(status_code=400, detail="Every exported track must include a part_id.")
        if not 0 <= float(track.volume) <= 1:
            raise HTTPException(status_code=400, detail="Track volume must be between 0 and 1.")
    return selected


def _export_mix_track_metadata(track: ExportMixTrackRequest) -> dict[str, Any]:
    return {
        "key": track.key,
        "label": track.label,
        "part_id": track.part_id.strip(),
        "job_id": track.job_id.strip(),
        "verse_number": track.verse_number,
        "muted": track.muted,
        "solo": track.solo,
        "volume": float(track.volume),
    }


async def _build_export_mix_billing_context(
    *,
    settings: Settings,
    sessions: SessionStore,
    job_store: JobStore,
    session_id: str,
    user_id: str,
    all_tracks: list[ExportMixTrackRequest],
    selected_tracks: list[ExportMixTrackRequest],
    billing_reference_job_id: str,
) -> dict[str, Any]:
    from src.backend.credits import estimate_export_mix_credits

    reference_job_id = billing_reference_job_id.strip()
    if not reference_job_id:
        raise HTTPException(status_code=400, detail="billing_reference_job_id is required.")
    reference_track = next(
        (track for track in all_tracks if track.job_id.strip() == reference_job_id),
        None,
    )
    if reference_track is None:
        raise HTTPException(
            status_code=400,
            detail="billing_reference_job_id must match one of the submitted tracks.",
        )
    work_dir = Path(tempfile.mkdtemp(prefix=f"export-billing-{session_id}-", dir=settings.data_dir))
    try:
        reference_job = await asyncio.to_thread(
            job_store.get_job_by_id,
            job_id=reference_job_id,
            user_id=user_id,
            session_id=session_id,
        )
        if reference_job is None:
            raise HTTPException(status_code=400, detail="Billing reference job was not found.")
        _ref_id, reference_data = reference_job
        _validate_export_mix_source_job(
            source_job_id=reference_job_id,
            source_data=reference_data,
            requested_part_id=reference_track.part_id.strip(),
        )
        reference_duration = await _export_mix_job_duration_seconds(
            settings=settings,
            sessions=sessions,
            session_id=session_id,
            user_id=user_id,
            source_job_id=reference_job_id,
            source_data=reference_data,
            work_dir=work_dir,
            requested_part_id=reference_track.part_id.strip(),
        )
        selected_durations: dict[str, float] = {}
        for track in selected_tracks:
            source_job_id = track.job_id.strip()
            source_job = await asyncio.to_thread(
                job_store.get_job_by_id,
                job_id=source_job_id,
                user_id=user_id,
                session_id=session_id,
            )
            if source_job is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Source synthesis job not found: {source_job_id}",
                )
            _source_id, source_data = source_job
            selected_durations[source_job_id] = await _export_mix_job_duration_seconds(
                settings=settings,
                sessions=sessions,
                session_id=session_id,
                user_id=user_id,
                source_job_id=source_job_id,
                source_data=source_data,
                work_dir=work_dir,
                requested_part_id=track.part_id.strip(),
            )
        tolerance_seconds = 1.0
        longer_duration = max(selected_durations.values(), default=reference_duration)
        if longer_duration > reference_duration + tolerance_seconds:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot use the billing reference track because another exported "
                    "track is longer."
                ),
            )
        required_credits = estimate_export_mix_credits(reference_duration)
        return {
            "billing_reference_job_id": reference_job_id,
            "billable_duration_seconds": reference_duration,
            "required_credits": required_credits,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def _export_mix_job_duration_seconds(
    *,
    settings: Settings,
    sessions: SessionStore,
    session_id: str,
    user_id: str,
    source_job_id: str,
    source_data: dict[str, Any],
    work_dir: Path,
    requested_part_id: str,
) -> float:
    _validate_export_mix_source_job(
        source_job_id=source_job_id,
        source_data=source_data,
        requested_part_id=requested_part_id,
    )
    duration = source_data.get("actualDurationSeconds")
    if isinstance(duration, (int, float)) and duration > 0:
        return float(duration)
    source_path = await _resolve_export_mix_source_path(
        settings=settings,
        sessions=sessions,
        session_id=session_id,
        user_id=user_id,
        source_job_id=source_job_id,
        source_data=source_data,
        work_dir=work_dir,
        requested_part_id=requested_part_id,
    )
    return await asyncio.to_thread(get_audio_duration_seconds, source_path)


async def _release_export_mix_reservation(
    *,
    settings: Settings,
    job_store: JobStore,
    user_id: str,
    job_id: str,
    error_message: str,
):
    from src.backend.credits import mark_reservation_reconciliation_required, release_credits

    release_result = await retry_credit_op(
        release_credits,
        user_id,
        job_id,
        max_attempts=settings.credit_retry_max_attempts,
        base_delay=settings.credit_retry_base_delay_seconds,
    )
    if release_result.status in {"released", "already_released", "reservation_missing"}:
        await asyncio.to_thread(
            job_store.update_job,
            job_id,
            status="failed",
            step="error",
            progress=1.0,
            errorMessage=error_message,
            jobKind="export_mix",
            billing={"reservationStatus": "released"},
        )
        return release_result
    await asyncio.to_thread(
        mark_reservation_reconciliation_required,
        user_id,
        job_id,
        last_error="export_mix_release_failed",
        last_error_message=(
            f"{error_message} | billing_rollback_status={release_result.status}"
        ),
    )
    await asyncio.to_thread(
        job_store.update_job,
        job_id,
        status="credit_reconciliation_required",
        step="error",
        progress=1.0,
        errorMessage=(
            f"{error_message} | billing_rollback_status={release_result.status}"
        ),
        jobKind="export_mix",
        billing={"reservationStatus": "reconciliation_required"},
    )
    return release_result


async def _run_export_mix_job(
    *,
    settings: Settings,
    sessions: SessionStore,
    job_store: JobStore,
    session_id: str,
    user_id: str,
    job_id: str,
    tracks: list[ExportMixTrackRequest],
    billable_duration_seconds: float,
) -> None:
    log = get_logger("backend.api")
    set_log_context(session_id=session_id, user_id=user_id, job_id=job_id)
    work_dir = settings.data_dir / "export-mix" / job_id
    output_path = sessions.session_dir(session_id) / f"mix-{job_id}.wav"
    output_storage_path = (
        f"sessions/{user_id}/{session_id}/jobs/{job_id}/mix.wav"
        if settings.backend_use_storage
        else None
    )
    try:
        log.info(
            "mix_export_job_running session_id=%s user_id=%s job_id=%s tracks=%s",
            session_id,
            user_id,
            job_id,
            len(tracks),
        )
        await asyncio.to_thread(
            job_store.update_job,
            job_id,
            status="running",
            step="export_mix",
            progress=0.02,
            jobKind="export_mix",
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        sources: list[MixTrackSource] = []
        for index, track in enumerate(tracks):
            source_job_id = track.job_id.strip()
            source_job = await asyncio.to_thread(
                job_store.get_job_by_id,
                job_id=source_job_id,
                user_id=user_id,
                session_id=session_id,
            )
            if source_job is None:
                raise ValueError(f"Source synthesis job not found: {source_job_id}")
            _source_id, source_data = source_job
            source_path = await _resolve_export_mix_source_path(
                settings=settings,
                sessions=sessions,
                session_id=session_id,
                user_id=user_id,
                source_job_id=source_job_id,
                source_data=source_data,
                work_dir=work_dir,
                requested_part_id=track.part_id.strip(),
            )
            sources.append(
                MixTrackSource(
                    key=track.key or f"id:{track.part_id.strip()}",
                    part_id=track.part_id.strip(),
                    label=track.label or track.part_id.strip(),
                    source_path=source_path,
                    volume=float(track.volume),
                )
            )
            await asyncio.to_thread(
                job_store.update_job,
                job_id,
                progress=0.05 + 0.1 * ((index + 1) / len(tracks)),
            )
            log.info(
                "mix_export_source_resolved session_id=%s user_id=%s job_id=%s "
                "source_job_id=%s part_id=%s",
                session_id,
                user_id,
                job_id,
                source_job_id,
                track.part_id.strip(),
            )

        last_reported_percent = 15

        def _report_render_progress(progress: float) -> None:
            nonlocal last_reported_percent
            progress_value = 0.15 + max(0.0, min(1.0, progress)) * 0.75
            percent = int(progress_value * 100)
            if percent < last_reported_percent + 5 and progress_value < 0.9:
                return
            last_reported_percent = percent
            job_store.update_job(job_id, progress=round(progress_value, 3))

        mix_metadata = await asyncio.to_thread(
            render_mix_to_wav,
            sources,
            output_path,
            progress_callback=_report_render_progress,
        )
        if output_storage_path:
            await asyncio.to_thread(
                upload_file,
                settings.storage_bucket,
                output_path,
                output_storage_path,
                "audio/wav",
            )
        final_mix_metadata = {
            **mix_metadata,
            "format": "wav",
            "trackCount": len(sources),
            "tracks": [_export_mix_track_metadata(track) for track in tracks],
        }
        from src.backend.credits import settle_export_mix_credits_and_complete_job

        settle_result = await retry_credit_op(
            settle_export_mix_credits_and_complete_job,
            user_id,
            job_id,
            session_id,
            billable_duration_seconds,
            actual_duration_seconds=mix_metadata.get("duration_seconds")
            if isinstance(mix_metadata.get("duration_seconds"), (int, float))
            else None,
            output_path=output_storage_path
            or str(output_path.relative_to(settings.project_root)),
            audio_url=f"/sessions/{session_id}/audio?file={output_path.name}",
            mix_metadata=final_mix_metadata,
            max_attempts=settings.credit_retry_max_attempts,
            base_delay=settings.credit_retry_base_delay_seconds,
        )
        if settle_result.status not in {
            "completed_and_settled",
            "already_completed_and_settled",
        }:
            release_result = await _release_export_mix_reservation(
                settings=settings,
                job_store=job_store,
                user_id=user_id,
                job_id=job_id,
                error_message=f"Export mix settlement failed: {settle_result.status}",
            )
            if release_result.status in {"released", "already_released"}:
                return
            return
        log.info(
            "mix_export_job_completed session_id=%s user_id=%s job_id=%s output_path=%s "
            "duration_seconds=%s",
            session_id,
            user_id,
            job_id,
            output_storage_path or output_path,
            mix_metadata.get("duration_seconds"),
        )
    except Exception as exc:
        log.exception(
            "mix_export_job_failed session_id=%s user_id=%s job_id=%s",
            session_id,
            user_id,
            job_id,
        )
        await _release_export_mix_reservation(
            settings=settings,
            job_store=job_store,
            user_id=user_id,
            job_id=job_id,
            error_message=str(exc) or "Export mix failed.",
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        clear_log_context()


async def _resolve_export_mix_source_path(
    *,
    settings: Settings,
    sessions: SessionStore,
    session_id: str,
    user_id: str,
    source_job_id: str,
    source_data: dict[str, Any],
    work_dir: Path,
    requested_part_id: str,
) -> Path:
    _validate_export_mix_source_job(
        source_job_id=source_job_id,
        source_data=source_data,
        requested_part_id=requested_part_id,
    )
    output_path = source_data.get("losslessOutputPath") or source_data.get("outputPath")
    if not isinstance(output_path, str) or not output_path.strip():
        raise ValueError(f"Source job is missing losslessOutputPath/outputPath: {source_job_id}")
    output_path = output_path.strip()
    if settings.backend_use_storage:
        expected_prefix = f"sessions/{user_id}/{session_id}/jobs/{source_job_id}/"
        if not output_path.startswith(expected_prefix):
            raise ValueError(f"Source job output path is not in the expected storage prefix.")
        suffix = Path(output_path).suffix or ".wav"
        local_path = work_dir / f"source-{source_job_id}{suffix}"
        data = await asyncio.to_thread(download_bytes, settings.storage_bucket, output_path)
        local_path.write_bytes(data)
        return local_path
    candidate = Path(output_path)
    local_path = (candidate if candidate.is_absolute() else settings.project_root / candidate).resolve()
    session_dir = sessions.session_dir(session_id).resolve()
    try:
        local_path.relative_to(session_dir)
    except ValueError as exc:
        raise ValueError("Source job output path is outside the session directory.") from exc
    if not local_path.exists():
        raise ValueError(f"Source audio file is missing: {source_job_id}")
    return local_path


def _validate_export_mix_source_job(
    *,
    source_job_id: str,
    source_data: dict[str, Any],
    requested_part_id: str,
) -> None:
    status = str(source_data.get("status") or "").strip()
    if status != "completed":
        raise ValueError(f"Source job is not completed: {source_job_id}")
    job_kind = str(source_data.get("jobKind") or "").strip()
    if job_kind in {"preprocess", "export_mix"}:
        raise ValueError(f"Source job is not a synthesis job: {source_job_id}")
    audio_track = source_data.get("audioTrack")
    if not isinstance(audio_track, dict):
        raise ValueError(f"Source job is missing audio track metadata: {source_job_id}")
    job_part_id = str(audio_track.get("part_id") or "").strip()
    if not job_part_id or job_part_id != requested_part_id:
        raise ValueError(
            f"Source job part_id mismatch for {source_job_id}: "
            f"{job_part_id or '<missing>'} != {requested_part_id}"
        )


async def _get_session_or_404(
    sessions: SessionStore, session_id: str, user_id: Optional[str]
) -> Any:
    """Fetch a session or raise an HTTP error for auth/missing cases."""
    try:
        return await sessions.get_session(session_id, user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Not authorized for this session.") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Session not found.") from exc


async def _get_snapshot_or_404(
    sessions: SessionStore, session_id: str, user_id: Optional[str]
) -> Dict[str, Any]:
    """Fetch a score snapshot or raise an HTTP error for auth/missing cases."""
    try:
        return await sessions.get_snapshot(session_id, user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Not authorized for this session.") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Session not found.") from exc


def _extract_bearer_token(request: Request) -> str:
    """Extract a bearer token from the Authorization header."""
    auth_header = request.headers.get("authorization")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header.")
    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header.")
    return parts[1]


async def _get_user_id_or_401(request: Request) -> str:
    """Return the authenticated user ID or raise HTTP 401."""
    settings: Settings = request.app.state.settings
    if settings.backend_auth_disabled and settings.app_env.lower() in {"dev", "development", "local", "test"}:
        user_id = settings.dev_user_id
        set_log_context(user_id=user_id)
        return user_id
    token = _extract_bearer_token(request)
    try:
        claims = await asyncio.to_thread(verify_id_token_claims, token)
        user_id = str(claims["uid"])
        user_email = str(claims.get("email") or "")
        await _require_not_under_maintenance(request, user_id, user_email)
        set_log_context(user_id=user_id)
        return user_id
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Firebase token.") from exc


async def _get_user_context_or_401(request: Request) -> tuple[str, str]:
    """Return the authenticated user ID and email, or raise HTTP 401."""
    user_id, user_email = await _get_user_context_without_maintenance_or_401(request)
    await _require_not_under_maintenance(request, user_id, user_email)
    return user_id, user_email


async def _get_user_context_without_maintenance_or_401(
    request: Request,
    *,
    prefer_token_when_auth_disabled: bool = False,
) -> tuple[str, str]:
    """Return authenticated user context without applying the maintenance access gate."""
    settings: Settings = request.app.state.settings
    if settings.backend_auth_disabled and settings.app_env.lower() in {"dev", "development", "local", "test"}:
        if prefer_token_when_auth_disabled and request.headers.get("authorization"):
            return await _get_verified_user_context_or_401(request)
        user_id = settings.dev_user_id
        set_log_context(user_id=user_id)
        return user_id, settings.dev_user_email

    return await _get_verified_user_context_or_401(request)


async def _get_verified_user_context_or_401(request: Request) -> tuple[str, str]:
    """Return user context from a verified Firebase bearer token."""
    token = _extract_bearer_token(request)
    try:
        claims = await asyncio.to_thread(verify_id_token_claims, token)
        user_id = str(claims["uid"])
        user_email = str(claims.get("email") or "")
        set_log_context(user_id=user_id)
        return user_id, user_email
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Firebase token.") from exc


async def _get_optional_verified_user_context(request: Request) -> tuple[str, str] | None:
    """Return verified user context when an Authorization header is present."""
    auth_header = request.headers.get("authorization")
    if not auth_header:
        return None
    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    try:
        claims = await asyncio.to_thread(verify_id_token_claims, parts[1])
        user_id = str(claims["uid"])
        user_email = str(claims.get("email") or "")
        return user_id, user_email
    except Exception:
        logger.warning("optional_auth_token_invalid")
        return None


async def _require_not_under_maintenance(request: Request, user_id: str, user_email: str) -> None:
    """Block app-owned user bootstrap paths during production maintenance."""
    settings: Settings = request.app.state.settings
    if settings.app_env.lower() in {"dev", "development", "local", "test"}:
        return
    try:
        config = await asyncio.to_thread(_get_maintenance_config)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Service availability could not be verified.",
        ) from exc
    enabled, allowed, message = _evaluate_maintenance_access(config, user_id, user_email)
    if not enabled or allowed:
        return
    raise HTTPException(
        status_code=503,
        detail=message or "SightSinger is temporarily under maintenance.",
    )


def _evaluate_maintenance_access(
    config: dict[str, Any],
    user_id: str,
    user_email: str,
) -> tuple[bool, bool, str | None]:
    enabled = bool(config.get("enabled"))
    if not enabled:
        return False, True, None
    allowed_uids = _normalize_string_set(config.get("allowedUids"))
    allowed_emails = {email.lower() for email in _normalize_string_set(config.get("allowedEmails"))}
    message = str(config.get("message") or "").strip()
    allowed = user_id in allowed_uids or user_email.strip().lower() in allowed_emails
    return True, allowed, message or "SightSinger is temporarily under maintenance."


def _get_maintenance_config() -> dict[str, Any]:
    snapshot = get_firestore_client().collection("app_config").document("maintenance").get()
    if not snapshot.exists:
        return {"enabled": False}
    data = snapshot.to_dict() or {}
    return data if isinstance(data, dict) else {"enabled": False}


def _normalize_string_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item.strip() for item in value if isinstance(item, str) and item.strip()}


async def _require_active_credits(user_id: str, user_email: str) -> None:
    """Block actions when the account is locked or credits are exhausted/expired."""
    from src.backend.credits import get_or_create_credits
    user_credits = await asyncio.to_thread(get_or_create_credits, user_id, user_email)
    if user_credits.overdrafted:
        raise HTTPException(
            status_code=403,
            detail="Account locked due to negative credit balance.",
        )
    if user_credits.is_expired:
        raise HTTPException(
            status_code=403,
            detail="Free trial credits have expired.",
        )
    if user_credits.available_balance <= 0:
        raise HTTPException(
            status_code=403,
            detail="No credits remaining.",
        )


async def _require_app_check(request: Request) -> None:
    """Enforce Firebase App Check on incoming requests."""
    settings: Settings = request.app.state.settings
    if not settings.backend_require_app_check:
        return
    initialize_firebase_app()
    token = request.headers.get("X-Firebase-AppCheck")
    if not token:
        raise HTTPException(status_code=401, detail="Missing App Check token.")
    try:
        await asyncio.to_thread(app_check.verify_token, token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid App Check token.") from exc


def _should_require_app_check(request: Request) -> bool:
    """Skip generic App Check where a route applies its own protection."""
    path = request.url.path
    if path in {"/auth/turnstile/verify", "/billing/webhook", "/healthz", "/readyz"}:
        return False
    return not (path.startswith("/sessions/") and path.endswith("/audio"))


def _client_ip(request: Request) -> str | None:
    """Return the best-effort client IP for abuse checks behind Cloud Run."""
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or None
    return request.client.host if request.client else None


def _backend_starting_http_exception(exc: McpStartupInProgressError) -> HTTPException:
    """Return the typed backend warmup response used by clients."""
    return HTTPException(
        status_code=503,
        detail={
            "code": exc.code,
            "message": exc.user_message,
        },
        headers={"Retry-After": "10"},
    )


async def _write_upload(path: Path, file: UploadFile, max_bytes: int) -> int:
    """Write an upload to disk while enforcing a size limit."""
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
        return total
    except HTTPException:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


def _normalize_uploaded_mxl(
    upload_path: Path,
    canonical_path: Path,
    *,
    max_mxl_uncompressed_bytes: int,
) -> None:
    """Normalize a zipped MusicXML upload to canonical XML on disk."""
    canonical_path.write_text(
        _read_musicxml_content(
            upload_path,
            max_mxl_uncompressed_bytes=max_mxl_uncompressed_bytes,
        ),
        encoding="utf-8",
    )


def _sign_audio_payload_urls(
    request: Request,
    payload: Dict[str, Any],
    *,
    user_id: str,
    resource_path: str | None = None,
) -> Dict[str, Any]:
    """Attach signed playback tokens to backend-issued audio URLs."""
    audio_url = payload.get("audio_url")
    if not isinstance(audio_url, str) or not audio_url:
        return payload
    signed = _build_signed_audio_url(
        request.app.state.settings,
        payload,
        user_id,
        audio_url,
        resource_path=resource_path,
    )
    if signed == audio_url:
        return payload
    updated = dict(payload)
    updated["audio_url"] = signed
    return updated


def _build_signed_audio_url(
    settings: Settings,
    payload: Dict[str, Any],
    user_id: str,
    audio_url: str,
    *,
    resource_path: str | None = None,
) -> str:
    """Append a short-lived playback token to a backend audio URL."""
    parts = urlsplit(audio_url)
    if not parts.path.startswith("/sessions/") or not parts.path.endswith("/audio"):
        return audio_url
    path_parts = [part for part in parts.path.split("/") if part]
    if len(path_parts) != 3:
        return audio_url
    session_id = path_parts[1]
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    file_name = query.get("file")
    if not file_name:
        return audio_url
    playback_resource_path = resource_path or _playback_resource_path(payload)
    playback_token = issue_playback_token(
        _load_playback_token_secret(settings),
        user_id=user_id,
        session_id=session_id,
        file_name=file_name,
        ttl_seconds=settings.playback_token_ttl_seconds,
        resource_path=playback_resource_path,
    )
    query["playback_token"] = playback_token
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _get_playback_claims_or_401(
    request: Request,
    settings: Settings,
    session_id: str,
    file_name: Optional[str],
) -> PlaybackTokenClaims:
    """Return verified playback claims or raise HTTP 401."""
    token = request.query_params.get("playback_token")
    if not token:
        raise HTTPException(status_code=401, detail="Missing playback token.")
    if not file_name:
        raise HTTPException(status_code=401, detail="Playback token requires an audio file name.")
    try:
        return verify_playback_token(
            token,
            _load_playback_token_secret(settings),
            session_id=session_id,
            file_name=file_name,
        )
    except PlaybackTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _load_playback_token_secret(settings: Settings) -> str:
    """Load the playback token signing secret using the standard secret pattern."""
    app_env = settings.app_env.lower()
    env_secret = os.getenv("BACKEND_PLAYBACK_TOKEN_VALUE", "").strip()
    if app_env in {"dev", "development", "local", "test"}:
        return env_secret or "dev-playback-token-secret"
    cache_key = (
        settings.project_id,
        settings.playback_token_secret_name,
        settings.playback_token_secret_version,
    )
    cached = _PLAYBACK_SECRET_CACHE.get(cache_key)
    if cached:
        return cached
    secret = read_secret(
        settings,
        settings.playback_token_secret_name,
        settings.playback_token_secret_version,
    )
    _PLAYBACK_SECRET_CACHE[cache_key] = secret
    return secret


def _playback_resource_path(payload: Dict[str, Any]) -> str | None:
    """Extract the exact backend resource identity for a playback URL."""
    candidate = payload.get("output_storage_path") or payload.get("outputPath")
    if isinstance(candidate, str) and candidate.strip():
        return candidate
    return None


def _session_input_storage_path(user_id: str, session_id: str, suffix: str) -> str:
    """Build the storage object path for a session upload."""
    safe_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    return f"sessions/{user_id}/{session_id}/input{safe_suffix}"


def _audio_media_type(storage_path: str) -> str:
    """Infer the audio media type from a file suffix."""
    suffix = Path(storage_path).suffix.lower()
    if suffix == ".wav":
        return "audio/wav"
    if suffix == ".mp3":
        return "audio/mpeg"
    return "application/octet-stream"


def _audio_response_headers(*, download: bool, file_name: str | None) -> dict[str, str]:
    """Build common headers for audio playback/download responses."""
    headers = {"Accept-Ranges": "bytes"}
    if download and file_name:
        headers["Content-Disposition"] = f'attachment; filename="{file_name}"'
    return headers


def _parse_byte_range(range_header: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single HTTP byte range."""
    if not range_header:
        return None
    value = range_header.strip()
    if not value.startswith("bytes="):
        raise HTTPException(status_code=416, detail="Invalid Range header.")
    spec = value[6:].strip()
    if "," in spec:
        raise HTTPException(status_code=416, detail="Multiple ranges are not supported.")
    if "-" not in spec:
        raise HTTPException(status_code=416, detail="Invalid Range header.")
    start_text, end_text = spec.split("-", 1)
    if not start_text:
        try:
            suffix_length = int(end_text)
        except ValueError as exc:
            raise HTTPException(status_code=416, detail="Invalid Range header.") from exc
        if suffix_length <= 0:
            raise HTTPException(status_code=416, detail="Invalid Range header.")
        if suffix_length >= size:
            return (0, size - 1)
        return (size - suffix_length, size - 1)
    try:
        start = int(start_text)
    except ValueError as exc:
        raise HTTPException(status_code=416, detail="Invalid Range header.") from exc
    if start < 0 or start >= size:
        raise HTTPException(status_code=416, detail="Requested range not satisfiable.")
    if not end_text:
        return (start, size - 1)
    try:
        end = int(end_text)
    except ValueError as exc:
        raise HTTPException(status_code=416, detail="Invalid Range header.") from exc
    if end < start:
        raise HTTPException(status_code=416, detail="Requested range not satisfiable.")
    return (start, min(end, size - 1))


async def _stream_storage_audio(
    request: Request,
    settings: Settings,
    storage_path: str,
    *,
    download: bool = False,
    file_name: str | None = None,
) -> Response:
    """Fetch audio bytes from storage and return a typed response."""
    media_type = _audio_media_type(storage_path)
    data = await asyncio.to_thread(download_bytes, settings.storage_bucket, storage_path)
    size = len(data)
    headers = _audio_response_headers(download=download, file_name=file_name)
    byte_range = _parse_byte_range(request.headers.get("range"), size)
    if byte_range is None:
        headers["Content-Length"] = str(size)
        return Response(content=data, media_type=media_type, headers=headers)
    start, end = byte_range
    content = data[start : end + 1]
    headers["Content-Length"] = str(len(content))
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return Response(
        content=content,
        status_code=206,
        media_type=media_type,
        headers=headers,
    )


def _read_musicxml_content(path: Path, *, max_mxl_uncompressed_bytes: int) -> str:
    """Read MusicXML content and map bounded archive failures to HTTP errors."""
    try:
        return read_musicxml_content_bounded(
            path,
            max_mxl_uncompressed_bytes=max_mxl_uncompressed_bytes,
        )
    except MusicXmlArchiveTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except MusicXmlArchiveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _resolve_session_score_path(
    settings: Settings, current_score: Any
) -> Optional[Path]:
    """Resolve the current session score artifact path, preferring derived MusicXML."""
    if not isinstance(current_score, dict):
        return None
    score_payload = current_score.get("score")
    if not isinstance(score_payload, dict):
        return None
    source_musicxml_path = score_payload.get("source_musicxml_path")
    if not isinstance(source_musicxml_path, str) or not source_musicxml_path.strip():
        return None
    return _resolve_allowlisted_score_path(settings, source_musicxml_path)


def _resolve_allowlisted_score_path(settings: Settings, candidate: str | Path) -> Path:
    """Resolve a score path and require that it stays within approved backend roots."""
    score_path = Path(candidate)
    if not score_path.is_absolute():
        score_path = settings.project_root / score_path
    resolved = score_path.resolve()
    approved_roots = (
        settings.sessions_dir.resolve(),
        settings.data_dir.resolve(),
    )
    for root in approved_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise HTTPException(status_code=403, detail="Score path is outside allowed roots.")


def _iter_file(path: Path, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    """Iterate over a file in fixed-size chunks."""
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            yield chunk

app = create_app()
