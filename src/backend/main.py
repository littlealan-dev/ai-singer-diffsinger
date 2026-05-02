from __future__ import annotations

"""FastAPI entrypoint for the backend service."""

from pathlib import Path
from typing import Any, Dict, AsyncIterator, Iterator, Optional
import asyncio
from contextlib import asynccontextmanager
import time
import os
import shutil
import tempfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, EmailStr
import logging

from src.backend.config import Settings
from src.backend.llm_factory import create_llm_client
from src.backend.mcp_client import McpRouter, McpError
from src.backend.orchestrator import Orchestrator
from src.backend.job_store import JobStore, build_progress_payload
from src.backend.session import SessionStore, FirestoreSessionStore
from src.backend.firebase_app import (
    initialize_firebase_app,
    verify_id_token,
    verify_id_token_claims,
)
from src.backend.storage_client import download_bytes, upload_file
from src.backend.waitlist import subscribe_to_waitlist, verify_app_check_token
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


class ChatRequest(BaseModel):
    """Request payload for chat-based interactions."""
    message: str
    # Optional structured selector payload from UI widgets (for example verse dropdown).
    # Values are treated as authoritative user selections and avoid fragile text parsing.
    selection: dict[str, Any] | None = None


class WaitlistSubscribeRequest(BaseModel):
    """Request payload for waitlist subscriptions."""
    email: EmailStr
    first_name: str | None = None
    feedback: str | None = None
    gdpr_consent: bool
    consent_text: str
    source: str


class BillingCheckoutRequest(BaseModel):
    planKey: str


class BillingCheckoutSyncRequest(BaseModel):
    sessionId: str


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
        router.start()
        _log_onnx_providers()
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
        # Default local dev origins.
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
        """Attach per-request logging context and timings."""
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
            if request.method != "OPTIONS" and _should_require_app_check(request):
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
            await _write_upload(temp_upload_path, file, settings.max_upload_bytes)
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
            except McpError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            logger.info(
                "upload_musicxml_timing session_id=%s suffix=%s upload_write_ms=%.2f "
                "normalize_mxl_ms=%.2f parse_score_ms=%.2f",
                session_id,
                suffix,
                upload_write_ms,
                normalize_mxl_ms,
                parse_score_ms,
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
            )
            return _sign_audio_payload_urls(request, response, user_id=user_id)
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
        latest = await asyncio.to_thread(
            job_store.get_latest_job_by_session,
            user_id=user_id,
            session_id=session_id,
        )
        if latest is None:
            return {"status": "idle"}
        job_id, data = latest
        payload = build_progress_payload(job_id, data)
        return _sign_audio_payload_urls(
            request,
            payload,
            user_id=user_id,
            resource_path=data.get("outputPath") if isinstance(data.get("outputPath"), str) else None,
        )

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

    @app.post("/billing/refresh")
    async def refresh_billing_credits() -> Dict[str, int]:
        from src.backend.billing_refresh import run_credit_refresh

        return await asyncio.to_thread(run_credit_refresh)

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
        return {
            "success": result.success,
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
        return Response(content=content, media_type="application/xml")

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
        user_id = await asyncio.to_thread(verify_id_token, token)
        set_log_context(user_id=user_id)
        return user_id
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Firebase token.") from exc


async def _get_user_context_or_401(request: Request) -> tuple[str, str]:
    """Return the authenticated user ID and email, or raise HTTP 401."""
    settings: Settings = request.app.state.settings
    if settings.backend_auth_disabled and settings.app_env.lower() in {"dev", "development", "local", "test"}:
        user_id = settings.dev_user_id
        set_log_context(user_id=user_id)
        return user_id, settings.dev_user_email
    token = _extract_bearer_token(request)
    try:
        claims = await asyncio.to_thread(verify_id_token_claims, token)
        user_id = claims["uid"]
        user_email = claims.get("email") or ""
        set_log_context(user_id=user_id)
        return user_id, user_email
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Firebase token.") from exc


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
    """Skip App Check only for signed audio playback routes."""
    path = request.url.path
    if path == "/billing/webhook":
        return False
    return not (path.startswith("/sessions/") and path.endswith("/audio"))


async def _write_upload(path: Path, file: UploadFile, max_bytes: int) -> None:
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
