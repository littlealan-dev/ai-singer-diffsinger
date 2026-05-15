from __future__ import annotations

"""FastAPI entrypoint for the billing-only backend service."""

from typing import Any, Dict
import asyncio
import logging
import os
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from firebase_admin import app_check

from src.backend.config import Settings
from src.backend.firebase_app import (
    get_firestore_client,
    initialize_firebase_app,
    verify_id_token_claims,
)
from src.mcp.logging_utils import (
    clear_log_context,
    configure_logging,
    get_logger,
    set_log_context,
)


class BillingCheckoutRequest(BaseModel):
    planKey: str


class BillingCheckoutSyncRequest(BaseModel):
    sessionId: str


def create_billing_app() -> FastAPI:
    """Create a lightweight billing API app without synthesis worker startup."""
    configure_logging()
    settings = Settings.from_env()
    app = FastAPI(title="SightSinger Billing API", version="0.1.0")
    logger = get_logger("backend.billing_api")
    logger.setLevel(logging.DEBUG)
    app.state.settings = settings

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
    async def log_and_guard_requests(request: Request, call_next):
        start = time.monotonic()
        set_log_context()
        response = None
        logger.debug(
            "billing_http_request_start method=%s path=%s",
            request.method,
            request.url.path,
        )
        try:
            if request.method != "OPTIONS" and _should_require_app_check(request):
                await _require_app_check(request)
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        finally:
            clear_log_context()
        elapsed_ms = (time.monotonic() - start) * 1000.0
        logger.info(
            "billing_http_request_complete method=%s path=%s status=%s elapsed_ms=%.2f",
            request.method,
            request.url.path,
            getattr(response, "status_code", "?"),
            elapsed_ms,
        )
        return response

    @app.get("/healthz")
    async def healthz() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/billing/healthz")
    async def billing_healthz() -> Dict[str, str]:
        return {"status": "ok"}

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

    return app


def _extract_bearer_token(request: Request) -> str:
    auth_header = request.headers.get("authorization")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header.")
    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header.")
    return parts[1]


async def _get_user_context_or_401(request: Request) -> tuple[str, str]:
    settings: Settings = request.app.state.settings
    if settings.backend_auth_disabled and settings.app_env.lower() in {"dev", "development", "local", "test"}:
        user_id = settings.dev_user_id
        set_log_context(user_id=user_id)
        return user_id, settings.dev_user_email

    token = _extract_bearer_token(request)
    try:
        claims = await asyncio.to_thread(verify_id_token_claims, token)
        user_id = str(claims["uid"])
        user_email = str(claims.get("email") or "")
        await _require_not_under_maintenance(request, user_id, user_email)
        set_log_context(user_id=user_id)
        return user_id, user_email
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Firebase token.") from exc


async def _require_not_under_maintenance(request: Request, user_id: str, user_email: str) -> None:
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
    return {str(item).strip() for item in value if str(item).strip()}


async def _require_app_check(request: Request) -> None:
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
    return request.url.path not in {"/billing/webhook", "/healthz", "/billing/healthz"}


app = create_billing_app()
