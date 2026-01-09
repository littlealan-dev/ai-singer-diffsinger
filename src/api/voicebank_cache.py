from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional, Set
import os
import shutil
import tarfile

from src.backend.storage_client import list_blobs
from src.mcp.logging_utils import get_logger

logger = get_logger(__name__)


def _app_env() -> str:
    return os.getenv("APP_ENV") or os.getenv("ENV") or "dev"


def is_prod_env() -> bool:
    return _app_env().lower() not in {"dev", "development", "local", "test"}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _local_voicebanks_root() -> Path:
    return _project_root() / "assets" / "voicebanks"


def _voicebank_bucket() -> str:
    return os.getenv("VOICEBANK_BUCKET") or os.getenv("STORAGE_BUCKET") or ""


def _voicebank_prefix() -> str:
    prefix = os.getenv("VOICEBANK_PREFIX", "assets/voicebanks")
    return prefix.strip().strip("/")


def _cache_root() -> Path:
    return Path(os.getenv("VOICEBANK_CACHE_DIR", "/tmp/voicebanks"))


def resolve_voicebank_path(voicebank_id: str) -> Path:
    if not voicebank_id:
        raise ValueError("voicebank is required.")
    if "/" in voicebank_id or "\\" in voicebank_id:
        raise ValueError("voicebank must be an ID (directory name).")
    if is_prod_env():
        return _ensure_cached_voicebank(voicebank_id)
    return _resolve_local_voicebank(voicebank_id)


def list_voicebank_ids() -> List[str]:
    if is_prod_env():
        return _list_voicebank_ids_gcs()
    return _list_voicebank_ids_local()


def _resolve_local_voicebank(voicebank_id: str) -> Path:
    root = _local_voicebanks_root()
    candidate = (root / voicebank_id).resolve()
    if root not in candidate.parents:
        raise ValueError("voicebank ID resolves outside voicebank root.")
    config_path = candidate / "dsconfig.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Voicebank not found: {voicebank_id}")
    return candidate


def _list_voicebank_ids_local() -> List[str]:
    root = _local_voicebanks_root()
    if not root.exists():
        return []
    ids: List[str] = []
    for item in root.iterdir():
        if item.is_dir() and (item / "dsconfig.yaml").exists():
            ids.append(item.name)
    return sorted(ids)


def _list_voicebank_ids_gcs() -> List[str]:
    bucket = _voicebank_bucket()
    if not bucket:
        raise ValueError("VOICEBANK_BUCKET or STORAGE_BUCKET is required in prod.")
    prefix = _voicebank_prefix()
    if prefix:
        prefix = f"{prefix}/"
    ids: Set[str] = set()
    for blob in list_blobs(bucket, prefix=prefix):
        name = blob.name
        if not name.startswith(prefix):
            continue
        remainder = name[len(prefix):]
        if not remainder or "/" in remainder:
            continue
        if not remainder.endswith(".tar.gz"):
            continue
        voicebank_id = remainder[: -len(".tar.gz")]
        if voicebank_id:
            ids.add(voicebank_id)
    return sorted(ids)


def _ensure_cached_voicebank(voicebank_id: str) -> Path:
    cache_root = _cache_root()
    target_dir = cache_root / voicebank_id
    config_path = target_dir / "dsconfig.yaml"
    if config_path.exists():
        return target_dir

    bucket = _voicebank_bucket()
    if not bucket:
        raise ValueError("VOICEBANK_BUCKET or STORAGE_BUCKET is required in prod.")
    prefix = _voicebank_prefix()
    archive_name = f"{voicebank_id}.tar.gz"
    archive_object = f"{prefix}/{archive_name}" if prefix else archive_name
    blob = None
    for candidate in list_blobs(bucket, prefix=archive_object):
        if candidate.name == archive_object:
            blob = candidate
            break
    if blob is None:
        raise FileNotFoundError(f"Voicebank not found in storage: {voicebank_id}")

    tmp_dir = cache_root / f".{voicebank_id}.tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    archive_path = tmp_dir / archive_name
    blob.download_to_filename(str(archive_path))
    _extract_tarball(archive_path, tmp_dir)

    config_path = tmp_dir / "dsconfig.yaml"
    if not config_path.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise FileNotFoundError(f"Voicebank missing dsconfig.yaml: {voicebank_id}")

    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir.replace(target_dir)
    return target_dir


def _extract_tarball(archive_path: Path, dest_dir: Path) -> None:
    base_dir = dest_dir.resolve()
    with tarfile.open(archive_path, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            member_path = dest_dir / member.name
            resolved = member_path.resolve()
            if base_dir not in resolved.parents and resolved != base_dir:
                raise ValueError("Tar archive contains unsafe paths.")
        tar.extractall(dest_dir)
