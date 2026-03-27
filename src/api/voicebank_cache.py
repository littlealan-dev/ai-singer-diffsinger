from __future__ import annotations

"""Helpers for resolving and caching voicebanks locally or from storage."""

from functools import lru_cache
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set
import os
import shutil
import tarfile
import time
import yaml

from src.backend.storage_client import list_blobs
from src.mcp.logging_utils import get_logger

logger = get_logger(__name__)


def _app_env() -> str:
    """Return the current environment name for prod/dev branching."""
    return os.getenv("APP_ENV") or os.getenv("ENV") or "dev"


def is_prod_env() -> bool:
    """Return True when running in a production-like environment."""
    return _app_env().lower() not in {"dev", "development", "local", "test"}


def _project_root() -> Path:
    """Return project root based on this file location."""
    return Path(__file__).resolve().parents[2]


def _local_voicebanks_root() -> Path:
    """Return the local assets voicebank root path."""
    return _project_root() / "assets" / "voicebanks"


def _voicebank_registry_path() -> Path:
    """Return the registered-voicebank config path."""
    override = os.getenv("VOICEBANK_REGISTRY_PATH")
    if override:
        return Path(override)
    return _project_root() / "src" / "backend" / "config" / "voicebank_registry.yaml"


@lru_cache(maxsize=1)
def _load_registered_voicebank_entries_for_path(registry_path: str) -> List[Dict[str, Any]]:
    """Return the configured registered voicebank entries."""
    path = Path(registry_path)
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("voicebank_registry_unreadable path=%s", path)
        return []
    entries = data.get("voicebanks") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        voicebank_id = entry.get("id")
        if isinstance(voicebank_id, str) and voicebank_id:
            normalized.append(dict(entry))
    return normalized


@lru_cache(maxsize=1)
def _load_registered_voicebank_ids_for_path(registry_path: str) -> List[str]:
    """Return the configured allowlist of supported voicebank IDs."""
    return [
        str(entry["id"])
        for entry in _load_registered_voicebank_entries_for_path(registry_path)
        if isinstance(entry.get("id"), str) and entry.get("id")
    ]


def _load_registered_voicebank_ids() -> List[str]:
    return _load_registered_voicebank_ids_for_path(str(_voicebank_registry_path().resolve()))


def get_registered_voicebank_metadata(voicebank_id: str) -> Dict[str, Any]:
    """Return manual registry metadata for one voicebank id."""
    if not voicebank_id:
        return {}
    registry_path = str(_voicebank_registry_path().resolve())
    for entry in _load_registered_voicebank_entries_for_path(registry_path):
        if entry.get("id") == voicebank_id:
            return {k: v for k, v in entry.items() if k != "id"}
    return {}


def resolve_registered_voicebank_id(voicebank: str | Path) -> Optional[str]:
    """Resolve a registry voicebank id from a plain id or a nested voicebank path."""
    registered_ids = set(_load_registered_voicebank_ids())
    if not registered_ids:
        return None

    if isinstance(voicebank, str) and "/" not in voicebank and "\\" not in voicebank:
        return voicebank if voicebank in registered_ids else None

    path = Path(voicebank).resolve()
    for candidate in (path, *path.parents):
        if candidate.name in registered_ids:
            return candidate.name
    return None


def discover_voicebank_root(base_dir: Path) -> Optional[Path]:
    """Find the actual usable voicebank root inside a top-level folder."""
    direct = base_dir / "dsconfig.yaml"
    if direct.exists():
        return base_dir

    candidates: List[Path] = []
    skip_dirs = {"dsdur", "dsmain", "dspitch", "dsvariance", "dsvocoder"}
    for config_path in base_dir.rglob("dsconfig.yaml"):
        parent = config_path.parent
        if parent.name in skip_dirs:
            continue
        if not ((parent / "character.yaml").exists() or (parent / "character.txt").exists()):
            continue
        candidates.append(parent)

    if not candidates:
        return None
    candidates.sort(key=lambda item: (len(item.relative_to(base_dir).parts), str(item)))
    return candidates[0]


def _voicebank_bucket() -> str:
    """Return the configured storage bucket for voicebanks."""
    return os.getenv("VOICEBANK_BUCKET") or os.getenv("STORAGE_BUCKET") or ""


def _voicebank_prefix() -> str:
    """Return the object prefix for voicebank archives in storage."""
    prefix = os.getenv("VOICEBANK_PREFIX", "assets/voicebanks")
    return prefix.strip().strip("/")


def _cache_root() -> Path:
    """Return the cache directory for downloaded voicebanks."""
    return Path(os.getenv("VOICEBANK_CACHE_DIR", "/tmp/voicebanks"))


def _stage_subdir(stage: str) -> Optional[str]:
    """Return the canonical subdirectory for a voicebank stage."""
    normalized = (stage or "").strip().lower()
    if normalized == "root":
        return None
    if normalized == "dur":
        return "dsdur"
    if normalized == "pitch":
        return "dspitch"
    if normalized == "variance":
        return "dsvariance"
    raise ValueError(f"Unsupported voicebank stage: {stage}")


def _load_yaml_dict(path: Path) -> Dict[str, Any]:
    """Load a YAML mapping or return an empty dict."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _resolve_stage_dir(voicebank_path: Path, stage: str) -> tuple[Path, bool]:
    """Resolve the directory that owns a stage's config and assets."""
    subdir = _stage_subdir(stage)
    if subdir is None:
        return voicebank_path, False
    candidate = voicebank_path / subdir
    if candidate.exists():
        return candidate, True
    return voicebank_path, False


def _resolve_inventory_from_dir(stage_dir: Path, stage_config: Dict[str, Any]) -> Optional[Path]:
    """Resolve a phoneme inventory path from one stage directory."""
    phonemes_ref = stage_config.get("phonemes")
    if isinstance(phonemes_ref, str) and phonemes_ref.strip():
        explicit = (stage_dir / phonemes_ref).resolve()
        if explicit.exists():
            return explicit
    for candidate in (
        stage_dir / "phonemes.json",
        stage_dir / "dsmain" / "phonemes.json",
    ):
        if candidate.exists():
            return candidate.resolve()
    return None


@lru_cache(maxsize=256)
def _resolve_stage_phoneme_inventory_path_cached(voicebank_path: str, stage: str) -> str:
    """Resolve the phoneme inventory path for one stage."""
    voicebank_root = Path(voicebank_path).resolve()
    stage_dir, stage_override_exists = _resolve_stage_dir(voicebank_root, stage)
    stage_config = _load_yaml_dict(stage_dir / "dsconfig.yaml")
    inventory_path = _resolve_inventory_from_dir(stage_dir, stage_config)
    if inventory_path is not None:
        return str(inventory_path)

    normalized_stage = (stage or "").strip().lower()
    if normalized_stage == "root":
        raise FileNotFoundError(f"Root phoneme inventory not found for {voicebank_root}")

    if normalized_stage == "dur":
        return _resolve_stage_phoneme_inventory_path_cached(str(voicebank_root), "root")

    if stage_override_exists:
        raise FileNotFoundError(
            f"Stage phoneme inventory not found for {normalized_stage} at {stage_dir}"
        )

    return _resolve_stage_phoneme_inventory_path_cached(str(voicebank_root), "root")


def resolve_stage_phoneme_inventory_path(voicebank_path: Path | str, stage: str) -> Path:
    """Return the resolved phoneme inventory path for a stage."""
    return Path(
        _resolve_stage_phoneme_inventory_path_cached(str(Path(voicebank_path).resolve()), stage)
    )


@lru_cache(maxsize=256)
def _load_stage_phoneme_inventory_cached(
    voicebank_path: str,
    stage: str,
) -> tuple[tuple[str, int], ...]:
    """Load and cache a stage-local phoneme inventory."""
    inventory_path = resolve_stage_phoneme_inventory_path(Path(voicebank_path), stage)
    data = json.loads(inventory_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid phoneme inventory format at {inventory_path}")
    normalized: List[tuple[str, int]] = []
    for symbol, value in data.items():
        if not isinstance(symbol, str) or not symbol:
            raise ValueError(f"Invalid phoneme symbol in {inventory_path}")
        normalized.append((symbol, int(value)))
    return tuple(normalized)


def get_stage_phoneme_inventory(voicebank_path: Path | str, stage: str) -> Dict[str, int]:
    """Return a cached phoneme inventory mapping for one stage."""
    return dict(
        _load_stage_phoneme_inventory_cached(str(Path(voicebank_path).resolve()), stage)
    )


def resolve_voicebank_path(voicebank_id: str) -> Path:
    """Resolve a voicebank ID to a local path, caching in prod if needed."""
    if not voicebank_id:
        raise ValueError("voicebank is required.")
    if "/" in voicebank_id or "\\" in voicebank_id:
        raise ValueError("voicebank must be an ID (directory name).")
    registered_ids = _load_registered_voicebank_ids()
    if registered_ids and voicebank_id not in registered_ids:
        raise FileNotFoundError(f"Voicebank not registered: {voicebank_id}")
    if is_prod_env():
        return _ensure_cached_voicebank(voicebank_id)
    return _resolve_local_voicebank(voicebank_id)


def list_voicebank_ids() -> List[str]:
    """List available voicebank IDs for the current environment."""
    if is_prod_env():
        return _list_voicebank_ids_gcs()
    return _list_voicebank_ids_local()


def _resolve_local_voicebank(voicebank_id: str) -> Path:
    """Resolve a voicebank ID from the local assets directory."""
    root = _local_voicebanks_root().resolve()
    candidate = (root / voicebank_id).resolve()
    if root not in candidate.parents:
        raise ValueError("voicebank ID resolves outside voicebank root.")
    resolved = discover_voicebank_root(candidate)
    if resolved is None:
        raise FileNotFoundError(f"Voicebank not found: {voicebank_id}")
    return resolved


def _list_voicebank_ids_local() -> List[str]:
    """Return voicebank IDs from the local assets directory."""
    root = _local_voicebanks_root()
    if not root.exists():
        return []
    registered_ids = _load_registered_voicebank_ids()
    if registered_ids:
        ids = []
        for voicebank_id in registered_ids:
            candidate = root / voicebank_id
            if candidate.is_dir() and discover_voicebank_root(candidate) is not None:
                ids.append(voicebank_id)
        return sorted(ids)
    ids: List[str] = []
    for item in root.iterdir():
        if item.is_dir() and discover_voicebank_root(item) is not None:
            ids.append(item.name)
    return sorted(ids)


def _list_voicebank_ids_gcs() -> List[str]:
    """Return voicebank IDs by scanning storage for tarball archives."""
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
    registered_ids = _load_registered_voicebank_ids()
    if registered_ids:
        return sorted([voicebank_id for voicebank_id in registered_ids if voicebank_id in ids])
    return sorted(ids)


def _ensure_cached_voicebank(voicebank_id: str) -> Path:
    """Download and extract a voicebank archive into the local cache."""
    cache_root = _cache_root()
    target_dir = cache_root / voicebank_id
    resolved_cached = discover_voicebank_root(target_dir) if target_dir.exists() else None
    if resolved_cached is not None:
        logger.info("voicebank_cache_hit voicebank=%s path=%s", voicebank_id, resolved_cached)
        return resolved_cached

    start_total = time.monotonic()
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
        # Clean up any previous partial download.
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    archive_path = tmp_dir / archive_name
    download_start = time.monotonic()
    blob.download_to_filename(str(archive_path))
    download_ms = (time.monotonic() - download_start) * 1000.0
    logger.info(
        "voicebank_downloaded voicebank=%s bytes=%s elapsed_ms=%.2f",
        voicebank_id,
        archive_path.stat().st_size if archive_path.exists() else "-",
        download_ms,
    )
    extract_start = time.monotonic()
    _extract_tarball(archive_path, tmp_dir)
    extract_ms = (time.monotonic() - extract_start) * 1000.0
    logger.info("voicebank_extracted voicebank=%s elapsed_ms=%.2f", voicebank_id, extract_ms)

    resolved_tmp = discover_voicebank_root(tmp_dir)
    if resolved_tmp is None:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise FileNotFoundError(f"Voicebank missing dsconfig.yaml: {voicebank_id}")

    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir.replace(target_dir)
    total_ms = (time.monotonic() - start_total) * 1000.0
    logger.info("voicebank_cached voicebank=%s elapsed_ms=%.2f", voicebank_id, total_ms)
    resolved_target = discover_voicebank_root(target_dir)
    if resolved_target is None:
        raise FileNotFoundError(f"Voicebank missing dsconfig.yaml after cache move: {voicebank_id}")
    return resolved_target


def _extract_tarball(archive_path: Path, dest_dir: Path) -> None:
    """Extract a tar.gz archive with a basic path traversal guard."""
    base_dir = dest_dir.resolve()
    with tarfile.open(archive_path, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            member_path = dest_dir / member.name
            resolved = member_path.resolve()
            if base_dir not in resolved.parents and resolved != base_dir:
                raise ValueError("Tar archive contains unsafe paths.")
        tar.extractall(dest_dir)
