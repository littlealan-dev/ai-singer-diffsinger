"""The share service: public share links on their own slim Cloud Run service.

It serves only the share routes (src/backend/share_serving.py), and loads no MCP, LLM,
orchestrator or phonemizer code, so a cold start takes seconds instead of the API's ~20 s.
The API keeps the same routes, so a share link also works there.

    python3 -m uvicorn --factory src.backend.share_app:create_share_app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from firebase_admin import app_check

from src.backend.config import Settings
from src.backend.firebase_app import initialize_firebase_app, verify_id_token_claims
from src.backend.instrumental_midi_files import current_midi_signature, restore_published_midi
from src.backend.job_store import JobStore
from src.backend.session import FirestoreSessionStore, SessionStore
from src.backend.share_serving import add_share_routes, share_opened_without_app_check
from src.mcp.logging_utils import configure_logging, get_logger

logger = get_logger("backend.share")


def create_share_app() -> FastAPI:
    """Create the share service's application."""
    configure_logging()
    settings = Settings.from_env()
    store = SessionStore if settings.session_store == "memory" else FirestoreSessionStore
    sessions = store(
        project_root=settings.project_root,
        sessions_dir=settings.sessions_dir,
        ttl_seconds=settings.session_ttl_seconds,
        max_sessions=settings.max_sessions,
        backend_use_storage=settings.backend_use_storage,
        storage_bucket=settings.storage_bucket,
    )
    app = FastAPI(title="SightSinger share")
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.job_store = JobStore()

    cors_env = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    cors_origins = ([origin.strip() for origin in cors_env.split(",") if origin.strip()]
                    if cors_env else [f"http://{host}:{port}" for port in (5173, 5174, 5002, 5003)
                                      for host in ("localhost", "127.0.0.1")])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def require_app_check(request: Request, call_next):
        """App Check on the share data and files, as the API does; not on the page or its image."""
        path = request.url.path
        if (settings.backend_require_app_check and request.method != "OPTIONS"
                and path.startswith("/public/s/") and not share_opened_without_app_check(request)):
            token = request.headers.get("X-Firebase-AppCheck")
            if not token:
                return JSONResponse(status_code=401, content={"detail": "Missing App Check token."})
            initialize_firebase_app()
            try:
                await asyncio.to_thread(app_check.verify_token, token)
            except Exception:
                return JSONResponse(status_code=401, content={"detail": "Invalid App Check token."})
        return await call_next(request)

    async def optional_user(request: Request) -> Optional[tuple[str, str]]:
        """The signed-in viewer, so the owner is recognised and not counted as a view."""
        parts = request.headers.get("authorization", "").split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        try:
            claims = await asyncio.to_thread(verify_id_token_claims, parts[1])
            return str(claims["uid"]), str(claims.get("email") or "")
        except Exception:
            logger.warning("optional_auth_token_invalid")
            return None

    def midi_signature(*args: Any, **kwargs: Any) -> Any:
        return current_midi_signature(sessions, *args, **kwargs)

    def restore_midi(**kwargs: Any) -> Any:
        return restore_published_midi(settings, logger, **kwargs)

    add_share_routes(app, settings=settings, sessions=sessions, optional_user=optional_user,
                     midi_signature=midi_signature, restore_midi=restore_midi)

    @app.get("/healthz")
    async def healthz() -> Dict[str, str]:
        return {"status": "ok", "build": settings.backend_build_id}

    @app.get("/readyz")
    async def readyz() -> Dict[str, Any]:
        return {"status": "ready", "ready": True, "build": settings.backend_build_id}

    return app

