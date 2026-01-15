from __future__ import annotations

"""Google Secret Manager helpers."""

from typing import Optional

from src.backend.config import Settings


def _build_secret_resource(
    settings: Settings, secret_name: str, version: str
) -> str:
    """Build a Secret Manager resource path for a secret/version."""
    if "/" in secret_name:
        return secret_name
    if not settings.project_id:
        raise RuntimeError("PROJECT_ID is required to fetch secrets in non-dev mode.")
    return f"projects/{settings.project_id}/secrets/{secret_name}/versions/{version}"


def read_secret(settings: Settings, secret_name: str, version: str = "latest") -> str:
    """Read a secret value from Google Secret Manager."""
    try:
        from google.cloud import secretmanager
    except ImportError as exc:
        raise RuntimeError(
            "google-cloud-secret-manager is not installed. Install dependencies to use Secret Manager."
        ) from exc
    client = secretmanager.SecretManagerServiceClient()
    resource = _build_secret_resource(settings, secret_name, version)
    response = client.access_secret_version(name=resource)
    return response.payload.data.decode("utf-8")
