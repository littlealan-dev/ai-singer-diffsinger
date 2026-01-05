from __future__ import annotations

from typing import Optional
import os

import firebase_admin
from firebase_admin import auth, credentials, firestore
from google.auth.credentials import AnonymousCredentials

_app: Optional[firebase_admin.App] = None
_firestore_client: Optional[firestore.Client] = None


def _project_id() -> Optional[str]:
    return (
        os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GCLOUD_PROJECT")
        or os.getenv("PROJECT_ID")
    )


def initialize_firebase_app() -> firebase_admin.App:
    global _app
    if _app is not None:
        return _app
    try:
        _app = firebase_admin.get_app()
        return _app
    except ValueError:
        pass
    options = {}
    project_id = _project_id()
    if project_id:
        options["projectId"] = project_id
    service_account_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE")
    use_emulator = bool(
        os.getenv("FIRESTORE_EMULATOR_HOST")
        or os.getenv("FIREBASE_AUTH_EMULATOR_HOST")
        or os.getenv("FIREBASE_STORAGE_EMULATOR_HOST")
    )
    if service_account_path:
        credential = credentials.Certificate(service_account_path)
        _app = firebase_admin.initialize_app(credential, options or None)
    elif use_emulator:
        credential = AnonymousCredentials()
        _app = firebase_admin.initialize_app(credential, options or None)
    else:
        _app = firebase_admin.initialize_app(options=options or None)
    return _app


def get_firestore_client() -> firestore.Client:
    global _firestore_client
    if _firestore_client is None:
        initialize_firebase_app()
        _firestore_client = firestore.client()
    return _firestore_client


def verify_id_token(token: str) -> str:
    initialize_firebase_app()
    decoded = auth.verify_id_token(token)
    uid = decoded.get("uid")
    if not uid:
        raise ValueError("Missing uid in Firebase token.")
    return uid
