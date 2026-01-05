from __future__ import annotations

from pathlib import Path
from typing import Optional
import os

from google.auth.credentials import AnonymousCredentials
from google.cloud import storage

_storage_client: Optional[storage.Client] = None


def _project_id() -> Optional[str]:
    return (
        os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GCLOUD_PROJECT")
        or os.getenv("PROJECT_ID")
    )


def _storage_emulator_host() -> Optional[str]:
    host = os.getenv("STORAGE_EMULATOR_HOST") or os.getenv("FIREBASE_STORAGE_EMULATOR_HOST")
    if not host:
        return None
    if not host.startswith("http://") and not host.startswith("https://"):
        host = f"http://{host}"
    return host


def get_storage_client() -> storage.Client:
    global _storage_client
    if _storage_client is None:
        project_id = _project_id()
        emulator_host = _storage_emulator_host()
        if emulator_host:
            os.environ.setdefault("STORAGE_EMULATOR_HOST", emulator_host)
            _storage_client = storage.Client(
                project=project_id, credentials=AnonymousCredentials()
            )
        else:
            _storage_client = storage.Client(project=project_id)
    return _storage_client


def get_bucket(bucket_name: str) -> storage.Bucket:
    if not bucket_name:
        raise ValueError("Storage bucket name is required.")
    client = get_storage_client()
    return client.bucket(bucket_name)


def upload_file(
    bucket_name: str, source_path: Path, dest_path: str, content_type: Optional[str] = None
) -> None:
    bucket = get_bucket(bucket_name)
    blob = bucket.blob(dest_path)
    blob.upload_from_filename(str(source_path), content_type=content_type)


def upload_bytes(
    bucket_name: str, data: bytes, dest_path: str, content_type: Optional[str] = None
) -> None:
    bucket = get_bucket(bucket_name)
    blob = bucket.blob(dest_path)
    blob.upload_from_string(data, content_type=content_type)


def download_bytes(bucket_name: str, object_path: str) -> bytes:
    bucket = get_bucket(bucket_name)
    blob = bucket.blob(object_path)
    return blob.download_as_bytes()


def copy_blob(bucket_name: str, source_path: str, dest_path: str) -> None:
    bucket = get_bucket(bucket_name)
    source = bucket.blob(source_path)
    try:
        bucket.copy_blob(source, bucket, dest_path)
    except Exception:
        data = source.download_as_bytes()
        upload_bytes(bucket_name, data, dest_path)


def blob_exists(bucket_name: str, object_path: str) -> bool:
    bucket = get_bucket(bucket_name)
    return bucket.blob(object_path).exists()
