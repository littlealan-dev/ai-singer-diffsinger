from __future__ import annotations

"""File-based progress tracking utilities."""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, Optional


def _utc_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def read_progress(path: Path) -> Optional[Dict[str, Any]]:
    """Read a progress file from disk, returning None on errors."""
    try:
        payload = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def write_progress(
    path: Path,
    payload: Dict[str, Any],
    *,
    expected_job_id: Optional[str] = None,
) -> bool:
    """Write progress data to disk atomically, guarding job ownership."""
    if expected_job_id is not None:
        existing = read_progress(path)
        if existing and existing.get("job_id") not in (None, expected_job_id):
            return False
    data = dict(payload)
    # Ensure a stable timestamp exists for clients polling this file.
    data.setdefault("updated_at", _utc_iso())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(data), encoding="utf-8")
    tmp_path.replace(path)
    return True
