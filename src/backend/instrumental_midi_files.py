"""Instrumental MIDI files: their source signature, where they live in storage, and restoring one.

Kept apart from the orchestrator so a service that only serves published MIDI (the share
service) can check and restore a file without loading the orchestrator.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.backend.storage_client import download_bytes
from src.musicxml.instrument_programs import llm_program_assignments_from_summary
from src.musicxml.performance_midi_version import PERFORMANCE_MIDI_VERSION

INSTRUMENTAL_MIDI_SIGNATURE_METADATA_KEY = "instrumental_midi_source_signature"
# Where each published MIDI file lives in storage, so an instance whose disk
# never had it can restore it. Keyed by kind: "written" or "expanded".
INSTRUMENTAL_MIDI_STORAGE_PATH_KEYS = {
    "written": "instrumental_midi_original_storage_path",
    "expanded": "instrumental_midi_expanded_storage_path",
}


def instrumental_midi_storage_prefix(user_id: str, session_id: str) -> str:
    """Return the storage prefix every instrumental MIDI object of a session is under."""
    return f"sessions/{user_id}/{session_id}/instrumental-midi/"


def instrumental_midi_storage_path(
    user_id: str, session_id: str, signature: str, kind: str
) -> str:
    """Return the storage object of one MIDI file, keyed by its source signature.

    Identical content always maps to the same object, so a later job for the
    same instrumental music finds the file already there.
    """
    return f"{instrumental_midi_storage_prefix(user_id, session_id)}{signature}/{kind}.mid"


def instrumental_midi_source_signature(
    score_id: Optional[str],
    musicxml_path: Path,
    score_summary: Optional[Dict[str, Any]],
) -> str:
    """Identify instrumental MIDI by everything it is built from.

    The score version is not enough: it also advances on selection-only
    changes, such as switching lyric lines, which leave every instrumental note
    unchanged. The MIDI depends on the uploaded score, the instrumental parts'
    music, the score-wide timing (its tempo map comes from the whole score and
    it expands repeats), the playback presets and the MIDI format version, so
    it is keyed on those. Vocal edits, lyric lines and solfege lines leave it
    unchanged. A summary parsed before part signatures existed falls back to
    the whole MusicXML file.
    """
    digest = hashlib.sha256()
    for field in (f"performance-midi-{PERFORMANCE_MIDI_VERSION}", str(score_id or "")):
        digest.update(field.encode("utf-8"))
        digest.update(b"\x1f")
    summary = score_summary or {}
    timing = summary.get("timing_signature")
    instrumental_signatures = [
        part.get("part_signature")
        for part in summary.get("parts") or []
        if isinstance(part, dict) and part_feeds_instrumental_midi(part)
    ]
    if timing and all(instrumental_signatures):
        for field in [str(timing), *instrumental_signatures]:
            digest.update(str(field).encode("utf-8"))
            digest.update(b"\x1f")
    else:
        digest.update(Path(musicxml_path).read_bytes())
        digest.update(b"\x1f")
    assignments = llm_program_assignments_from_summary(summary)
    digest.update(json.dumps(assignments, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()[:24]


def part_feeds_instrumental_midi(part: Dict[str, Any]) -> bool:
    """A part that can become an instrumental MIDI track, including a user-assignable drum part."""
    return any(
        isinstance(instrument, dict)
        and (
            instrument.get("eligible_for_instrumental_midi")
            or instrument.get("instrumental_candidate")
            or instrument.get("user_assignable")
        )
        for instrument in part.get("instruments") or []
    )


async def current_midi_signature(
    sessions: Any,
    session_id: str,
    *,
    user_id: Optional[str],
    score_id: Optional[str],
    score_summary: Optional[Dict[str, Any]],
    musicxml_path: Optional[Path] = None,
) -> str:
    """Return the source signature of the session's instrumental MIDI."""
    if musicxml_path is None:
        musicxml_path = await sessions.ensure_active_musicxml(session_id, user_id)
    return await asyncio.to_thread(
        instrumental_midi_source_signature, score_id, musicxml_path, score_summary
    )


async def restore_published_midi(
    settings: Any,
    logger: logging.Logger,
    *,
    user_id: Optional[str],
    session_id: str,
    files: Dict[str, Any],
    kind: str,
    local_path: Path,
    download: Callable[[str, str], bytes] = download_bytes,
) -> bool:
    """Make a published MIDI file available on this instance's disk.

    Returns True when the file is there, restoring it from storage when
    another instance published it. Returns False when it cannot be had.
    """
    if local_path.is_file():
        return True
    if not settings.backend_use_storage or not settings.storage_bucket:
        return False
    object_path = files.get(INSTRUMENTAL_MIDI_STORAGE_PATH_KEYS[kind])
    if not user_id or not isinstance(object_path, str) or not object_path.startswith(
        instrumental_midi_storage_prefix(user_id, session_id)
    ):
        return False
    temporary_path = local_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        data = await asyncio.to_thread(download, settings.storage_bucket, object_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_bytes(data)
        temporary_path.replace(local_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        logger.warning(
            "instrumental_midi_restore_failed session=%s object=%s",
            session_id,
            object_path,
            exc_info=True,
        )
        return False
    logger.info(
        "instrumental_midi_restored session=%s object=%s bytes=%s",
        session_id,
        object_path,
        len(data),
    )
    return True
