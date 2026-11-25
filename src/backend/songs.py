from __future__ import annotations

"""Songs: a session with a score, its saved player and the jobs it shows.

The rules here are shared by both session stores and the endpoints. The
player record lives at sessions/{id}/player/current (Firestore) or on the
session (memory). Fields are stored camelCase; the API answers snake_case.
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field


SONG_TITLE_MAX_CHARS = 120
UNTITLED_SONG = "Untitled song"
DEFAULT_VOCAL_VOLUME = 1.0
_ACTIVE_STATUSES = {"queued", "running"}
_TAKE_JOB_KINDS = {"", "synthesis"}


class SongScoreChangedError(Exception):
    """A mix was saved for a score the Song no longer holds."""


def song_title_from(summary: Any, file_name: Optional[str]) -> str:
    """The score's title, else the file name without its extension."""
    candidates = [
        summary.get("title") if isinstance(summary, dict) else None,
        Path(file_name).stem if file_name else None,
    ]
    for candidate in candidates:
        title = str(candidate or "").strip()
        if title:
            return title[:SONG_TITLE_MAX_CHARS]
    return UNTITLED_SONG


def normalize_song_title(raw: Any) -> Optional[str]:
    """A user's title trimmed, or None when it is empty or too long."""
    if not isinstance(raw, str):
        return None
    title = raw.strip()
    if not title or len(title) > SONG_TITLE_MAX_CHARS:
        return None
    return title


def song_fields_at_upload(
    session: Dict[str, Any], summary: Any, file_name: Optional[str], now: Any, *,
    score_info: Optional[Dict[str, str]] = None, producer: Optional[str] = None,
) -> Dict[str, Any]:
    """Session updates for an upload: a first upload makes the session a Song.

    Song info is filled from the score at the first upload, with the producer's name.
    A later upload refills only the score's fields the owner hasn't edited.
    """
    from_score = {key: str((score_info or {}).get(key) or "")[:SONG_INFO_LIMITS[key]]
                  for key in SCORE_INFO_FIELDS}
    if not session.get("songCreatedAt"):
        return {
            "songCreatedAt": now, "songListed": True, "deletedAt": None,
            "titleEdited": False, "title": song_title_from(summary, file_name),
            "songInfo": {
                **from_score,
                "producer": str(producer or "").strip()[:SONG_INFO_LIMITS["producer"]],
                "description": "", "license": DEFAULT_LICENSE, "edited": [],
            },
        }
    fields: Dict[str, Any] = {}
    if not session.get("titleEdited"):
        fields["title"] = song_title_from(summary, file_name)
    info = dict(session.get("songInfo") or {})
    edited = set(info.get("edited") or [])
    info.update({key: value for key, value in from_score.items() if key not in edited})
    fields["songInfo"] = stored_song_info(info)
    return fields


# Song info (docs/specs/song-info-spec.md). Stored camelCase on the session as songInfo.
SCORE_INFO_FIELDS = ("subtitle", "composer", "lyricist", "arranger", "credits", "scoreCopyright")
SONG_INFO_LIMITS = {
    "subtitle": 200, "composer": 200, "lyricist": 200, "arranger": 200, "credits": 500,
    "scoreCopyright": 300, "producer": 120, "description": 2000,
}
SONG_LICENSES = ("all_rights_reserved", "cc_by_4", "public_domain")
DEFAULT_LICENSE = "all_rights_reserved"
LICENSE_TEXT = {
    "all_rights_reserved": ("All rights reserved", None),
    "cc_by_4": ("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"),
    "public_domain": ("Public domain (CC0)", "https://creativecommons.org/publicdomain/zero/1.0/"),
}
_INFO_API_KEYS = {
    "subtitle": "subtitle", "composer": "composer", "lyricist": "lyricist",
    "arranger": "arranger", "credits": "credits", "score_copyright": "scoreCopyright",
    "producer": "producer",
    "description": "description", "license": "license",
}


class InvalidSongInfo(ValueError):
    """A Song info edit the API refuses; its argument is the offending key."""


def stored_song_info(raw: Any) -> Dict[str, Any]:
    """A session's songInfo with every field present: empty text and the default license."""
    info = raw if isinstance(raw, dict) else {}
    stored: Dict[str, Any] = {key: str(info.get(key) or "") for key in SONG_INFO_LIMITS}
    stored["license"] = info.get("license") if info.get("license") in SONG_LICENSES else DEFAULT_LICENSE
    stored["edited"] = sorted({key for key in info.get("edited") or [] if key in _INFO_API_KEYS.values()})
    return stored


def normalize_song_info_edit(raw: Any) -> Dict[str, str]:
    """The owner's edits, keyed as stored, or InvalidSongInfo naming the first bad key."""
    if not isinstance(raw, dict):
        raise InvalidSongInfo("info")
    edits: Dict[str, str] = {}
    for key, value in raw.items():
        stored_key = _INFO_API_KEYS.get(key)
        if stored_key is None or not isinstance(value, str):
            raise InvalidSongInfo(str(key))
        if stored_key == "license":
            if value not in SONG_LICENSES:
                raise InvalidSongInfo(key)
            edits[stored_key] = value
            continue
        text = value.replace("\r\n", "\n").strip()
        if stored_key != "description":
            text = " ".join(text.split())
        if len(text) > SONG_INFO_LIMITS[stored_key]:
            raise InvalidSongInfo(key)
        edits[stored_key] = text
    return edits


def song_info_with_edits(raw: Any, edits: Dict[str, str]) -> Dict[str, Any]:
    """songInfo after the owner's edits, each edited key remembered so an upload keeps it."""
    info = stored_song_info(raw)
    info.update(edits)
    info["edited"] = sorted(set(info["edited"]) | set(edits))
    return info


def voice_names(tracks: Iterable[Dict[str, Any]]) -> List[str]:
    """The voicebank names of these sung tracks, each once, in track order."""
    names: List[str] = []
    for track in tracks:
        name = track.get("voiceName")
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def song_info_response(raw: Any, *, title: Optional[str], voices: List[str]) -> Dict[str, Any]:
    """The API's Song info: snake_case, with the title and the voices."""
    info = stored_song_info(raw)
    response: Dict[str, Any] = {"title": title or UNTITLED_SONG}
    response.update({api_key: info[stored_key] for api_key, stored_key in _INFO_API_KEYS.items()})
    response["voices"] = list(voices)
    return response


def empty_player(owner_uid: Optional[str], score_id: Optional[str]) -> Dict[str, Any]:
    return {
        "ownerUid": owner_uid, "scoreId": score_id, "revision": 0, "repeats": False,
        "vocals": [], "instruments": [],
        "bus": {"muted": False, "solo": False, "levelDb": 0.0},
    }


def is_take_job(job: Any) -> bool:
    """A sung take. Worker-run takes say jobKind "synthesis"; in-process ones say nothing."""
    return (
        isinstance(job, dict)
        and isinstance(job.get("audioTrack"), dict)
        and str(job.get("jobKind") or "") in _TAKE_JOB_KINDS
    )


def take_entry(job_id: str, job: Dict[str, Any]) -> Dict[str, Any]:
    track = job["audioTrack"]
    return {
        "key": str(track.get("key") or ""),
        "mode": track.get("mode") or "lyrics",
        "partId": track.get("part_id"),
        "partIndex": track.get("part_index"),
        "label": track.get("label"),
        "verseNumber": track.get("verse_number"),
        "takeSignature": track.get("take_signature"),
        "sourceJobId": job_id,
        "expandRepeats": bool(job.get("expandRepeats")),
        "durationSeconds": job.get("actualDurationSeconds"),
        "voiceName": job.get("voicebankName"),
        "muted": False, "solo": False, "volume": DEFAULT_VOCAL_VOLUME,
    }


def apply_take(player: Dict[str, Any], job_id: str, job: Dict[str, Any]) -> bool:
    """Put a settled take in the player. False when the record is unchanged."""
    if not is_take_job(job) or not take_entry(job_id, job)["key"]:
        return False
    if job.get("scoreId") != player.get("scoreId"):
        return False  # the score was replaced while it sang
    repeats = bool(job.get("expandRepeats"))
    vocals = list(player.get("vocals") or [])
    if repeats != bool(player.get("repeats")):
        vocals = []  # the player holds takes of one repeat order
    entry = take_entry(job_id, job)
    for index, old in enumerate(vocals):
        same_part = (old.get("partId") == entry["partId"]
                     if entry["partId"] else old.get("partIndex") == entry["partIndex"])
        if same_part and (old.get("mode") or "lyrics") == entry["mode"]:
            entry.update(muted=old.get("muted", False), solo=old.get("solo", False),
                         volume=old.get("volume", DEFAULT_VOCAL_VOLUME))
            if old == entry and repeats == bool(player.get("repeats")):
                return False
            vocals[index] = entry
            break
    else:
        vocals.append(entry)
    player["vocals"] = vocals
    player["repeats"] = repeats
    return True


def apply_sing_all_opening_balance(player: Dict[str, Any], job: Dict[str, Any]) -> bool:
    """Apply the package's balance between the voices to untouched Song mixer defaults.

    The takes are the same WAV files the server mixed, so the gaps between the
    voices carry over: in each mode sung, the voice with the largest gain opens
    at 1 and the others sit under it. The instruments keep the player's own
    defaults. The server's instrument gain makes up for FluidSynth's quiet render;
    the browser plays the instruments with its own synth, at a level nothing measures.
    """
    if player.get("scoreId") != job.get("scoreId"):
        return False
    stats = (job.get("package") or {}).get("stats") or {}
    # A package made before each mode had its own balance saved only the first mode's.
    balances = stats.get("balances") or [stats.get("balance") or {}]
    changed = False
    for balance in balances:
        changed = _apply_mode_balance(player, job, balance) or changed
    return changed


def _apply_mode_balance(player: Dict[str, Any], job: Dict[str, Any], balance: Dict[str, Any]) -> bool:
    stats = balance.get("stats") or {}
    mode = balance.get("mode")
    if mode not in {"lyrics", "solfege"}:
        return False
    vocals = stats.get("vocals") or []
    gains = [float(value["gain_db"]) for value in vocals
             if not value.get("dropped") and value.get("gain_db") is not None]
    if not gains:
        return False
    shift = max(gains)
    done = [item for item in job.get("items") or []
            if item.get("type") == "take" and item.get("status") == "done"
            and item.get("mode") == mode]
    by_job = {item.get("childJobId"): value for item, value in zip(done, vocals)
              if not value.get("dropped") and value.get("gain_db") is not None}
    changed = False
    for track in player.get("vocals") or []:
        value = by_job.get(track.get("sourceJobId"))
        # Per value: the owner's mute and solo stay; a volume still at Sing All's default is replaced.
        if value is None or track.get("volume", DEFAULT_VOCAL_VOLUME) != DEFAULT_VOCAL_VOLUME:
            continue
        volume = 10 ** ((float(value["gain_db"]) - shift) / 20)
        if track.get("volume") != volume:
            track["volume"] = volume
            changed = True
    return changed


class MixVocal(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    key: str = Field(max_length=200)
    muted: bool
    solo: bool
    # The player's vocal sliders go up to 1.2.
    volume: float = Field(ge=0, le=1.2)


class MixInstrument(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    raw_part_id: str = Field(max_length=200)
    muted: bool
    solo: bool
    volume: float = Field(ge=0, le=1)
    gm_program: Optional[int] = Field(default=None, ge=0, le=127)
    soundfont_bank: Optional[int] = Field(default=None, ge=0, le=16383)
    preset_kind: Optional[str] = Field(default=None, max_length=200)
    unassigned: bool = False


class MixBus(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    muted: bool
    solo: bool
    level_db: float = Field(ge=-48, le=6)


class PlayerMix(BaseModel):
    model_config = ConfigDict(extra="ignore")
    score_id: str = Field(max_length=200)
    vocals: List[MixVocal] = Field(max_length=64)
    instruments: List[MixInstrument] = Field(max_length=64)
    bus: MixBus


def apply_mix(player: Dict[str, Any], mix: PlayerMix) -> None:
    """Save the owner's mixer. Only a take adds a vocal track."""
    by_key = {vocal.key: vocal for vocal in mix.vocals}
    vocals = []
    for track in player.get("vocals") or []:
        saved = by_key.get(track.get("key"))
        if saved is not None:
            track = {**track, "muted": saved.muted, "solo": saved.solo, "volume": saved.volume}
        vocals.append(track)
    player["vocals"] = vocals
    player["instruments"] = [
        {
            "rawPartId": item.raw_part_id, "muted": item.muted, "solo": item.solo,
            "volume": item.volume, "gmProgram": item.gm_program,
            "soundfontBank": item.soundfont_bank, "presetKind": item.preset_kind,
            "unassigned": item.unassigned,
        }
        for item in mix.instruments
    ]
    player["bus"] = {"muted": mix.bus.muted, "solo": mix.bus.solo, "levelDb": mix.bus.level_db}


def workspace_jobs(jobs: Iterable[Tuple[str, Dict[str, Any]]]) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """The jobs a reopened Song needs, oldest first.

    A running job of any kind (Stop, polling, upload disabled) and each take
    asked for in the chat. Everything else is already in the saved chat text
    or isn't a chat item: a split that ended, a stopped take, an export mix.
    """
    for job_id, job in jobs:
        status = job.get("status")
        if status in _ACTIVE_STATUSES:
            yield job_id, job
        elif is_take_job(job) and job.get("originatingTurnId") and status in {"completed", "failed"}:
            yield job_id, job


def current_take_signatures(score_summary: Any) -> Dict[str, Any]:
    """Each part's take_signature in the score as it is now."""
    return {
        str(part.get("part_id")): part.get("take_signature")
        for part in (score_summary or {}).get("parts") or []
        if isinstance(part, dict) and part.get("part_id") and part.get("take_signature")
    }


def usable_vocals(
    player: Optional[Dict[str, Any]],
    *,
    score_id: Optional[str],
    score_summary: Any,
    jobs_by_id: Dict[str, Dict[str, Any]],
) -> Iterator[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """The player's tracks that still play: their audio exists and their music is unchanged."""
    if not player or player.get("scoreId") != score_id:
        return
    current = current_take_signatures(score_summary)
    for track in player.get("vocals") or []:
        job = jobs_by_id.get(str(track.get("sourceJobId") or ""))
        if job is None or job.get("status") != "completed" or not job.get("audioUrl"):
            continue
        recorded = track.get("takeSignature")
        now = current.get(str(track.get("partId") or ""))
        if recorded and now and recorded != now:
            continue  # the music was edited since
        yield track, job


def player_response(player: Optional[Dict[str, Any]], *, score_id: Optional[str]) -> Dict[str, Any]:
    """The record in API form, without vocals: the caller adds the usable ones."""
    record = player if player and player.get("scoreId") == score_id else empty_player(None, score_id)
    bus = record.get("bus") or {}
    return {
        "score_id": record.get("scoreId"),
        "revision": int(record.get("revision") or 0),
        "repeats": bool(record.get("repeats")),
        "vocals": [],
        "instruments": [
            {
                "raw_part_id": item.get("rawPartId"), "muted": bool(item.get("muted")),
                "solo": bool(item.get("solo")), "volume": item.get("volume"),
                "gm_program": item.get("gmProgram"), "soundfont_bank": item.get("soundfontBank"),
                "preset_kind": item.get("presetKind"), "unassigned": bool(item.get("unassigned")),
            }
            for item in record.get("instruments") or []
        ],
        "bus": {
            "muted": bool(bus.get("muted")), "solo": bool(bus.get("solo")),
            "level_db": bus.get("levelDb", 0.0),
        },
    }


def vocal_response(track: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "key": track.get("key"), "part_id": track.get("partId"),
        "mode": track.get("mode") or "lyrics",
        "part_index": track.get("partIndex"), "label": track.get("label"),
        "verse_number": track.get("verseNumber"), "take_signature": track.get("takeSignature"),
        "source_job_id": track.get("sourceJobId"), "expand_repeats": bool(track.get("expandRepeats")),
        "duration_seconds": track.get("durationSeconds"), "voice_name": track.get("voiceName"),
        "muted": bool(track.get("muted")), "solo": bool(track.get("solo")),
        "volume": track.get("volume", DEFAULT_VOCAL_VOLUME),
    }


def owned_job_path(path: Any, *, user_id: Any, session_id: Any, job_id: Any) -> Optional[str]:
    """A job's stored file, only when it is inside that job's own folder (storage mode)."""
    if not all(isinstance(value, str) and value and "/" not in value
               for value in (user_id, session_id, job_id)):
        return None
    prefix = f"sessions/{user_id}/{session_id}/jobs/{job_id}/"
    if isinstance(path, str) and path.startswith(prefix) and ".." not in path.split("/"):
        return path
    return None


def job_folder_id(path: Any, *, user_id: str, session_id: str) -> Optional[str]:
    """The job id of a stored path in this session's jobs folder, or None."""
    prefix = f"sessions/{user_id}/{session_id}/jobs/"
    if not isinstance(path, str) or not path.startswith(prefix):
        return None
    job_id = path[len(prefix):].split("/", 1)[0]
    return job_id or None


def iso_time(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return value if isinstance(value, str) else None
