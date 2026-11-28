"""Share links: the public routes and the file-serving helpers they share with the owner's routes.

Both the API (src/backend/main.py) and the slim share service (src/backend/share_app.py) add
these routes, so a share link behaves the same on either. This module loads no MCP, LLM or
phonemizer code: the share service starts in seconds.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from src.backend import share_preview, songs
from src.backend.config import Settings
from src.backend.instrumental_midi_files import INSTRUMENTAL_MIDI_SIGNATURE_METADATA_KEY
from src.backend.message_catalog import backend_message
from src.backend.score_thumbnail import (
    load_thumbnail, render_thumbnail, store_thumbnail, stored_version as stored_thumbnail_version,
)
from src.backend.session import SessionMusicXmlUnavailableError, SessionStore
from src.backend.storage_client import download_byte_range, object_size
from src.mcp.logging_utils import get_logger
from src.musicxml.io import (
    MusicXmlArchiveError,
    MusicXmlArchiveTooLargeError,
    read_musicxml_content as read_musicxml_content_bounded,
)

logger = get_logger("backend.api")

# The instrumental MIDI's current source signature, and putting a published MIDI file on this
# instance's disk: the orchestrator's methods in the API, light functions in the share service.
MidiSignature = Callable[..., Awaitable[str]]
RestoreMidi = Callable[..., Awaitable[bool]]
OptionalUser = Callable[[Request], Awaitable[Optional[tuple[str, str]]]]

# Stored audio is read from Cloud Storage this many bytes at a time.
STORAGE_AUDIO_CHUNK_BYTES = 1024 * 1024


def share_opened_without_app_check(request: Request) -> bool:
    """A share link's page and its preview image.

    A browser or a link-preview fetcher (WhatsApp, Slack) opens them and can't send App Check;
    both refuse a link that's off.
    """
    path = request.url.path
    return request.method == "GET" and (
        (path.startswith("/s/") and path.count("/") == 2)
        or (path.startswith("/public/s/") and path.endswith("/thumbnail.png"))
    )


def add_share_routes(
    app: FastAPI,
    *,
    settings: Settings,
    sessions: SessionStore,
    optional_user: OptionalUser,
    midi_signature: MidiSignature,
    restore_midi: RestoreMidi,
) -> None:
    """Add the public share routes: the page, its data, the score, MIDI, takes and thumbnail."""
    app.state.share_clock = time.monotonic
    share_requests: dict[tuple[str, str], deque[float]] = {}

    @app.middleware("http")
    async def public_share_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/public/s/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    async def resolve_share(token: str, request: Request, kind: str):
        """Resolve a live share while leaving the Song untouched."""
        share_settings: Settings = request.app.state.settings
        now = request.app.state.share_clock()
        key = (client_ip(request) or "unknown", kind)
        limit = (share_settings.share_audio_limit_per_minute if kind == "audio"
                 else share_settings.share_page_limit_per_minute)
        # Each request runs on the event loop and this section contains no await.
        for old_key, times in list(share_requests.items()):
            while times and now - times[0] >= 60:
                times.popleft()
            if not times:
                del share_requests[old_key]
        times = share_requests.setdefault(key, deque())
        if len(times) >= limit:
            return share_error(429, "rate_limited", "share.rate_limited", retry_after="60")
        times.append(now)
        if share_settings.share_links == "off" or not valid_share_token(token):
            return share_error(404, "share_off", "share.off")
        share = await sessions.get_share(token)
        if not share:
            return share_error(404, "share_off", "share.off")
        try:
            state = await sessions.read_shared(share.get("sessionId"))
        except (KeyError, PermissionError):
            return share_error(404, "share_off", "share.off")
        if state.share_token != token or state.user_id != share.get("ownerUid"):
            return share_error(404, "share_off", "share.off")
        return share, state

    async def count_share_use(token: str, *, play: bool = False) -> None:
        try:
            await sessions.count_share_use(token, play=play)
        except Exception:
            logger.warning("Could not update Share %s counter.", "play" if play else "view")

    @app.get("/public/s/{token}")
    async def public_share_page(token: str, request: Request) -> Any:
        resolved = await resolve_share(token, request, "page")
        if isinstance(resolved, Response):
            return resolved
        share, state = resolved
        player = await sessions.get_player(state.id, share["ownerUid"], shared=True)
        player_payload = songs.player_response(player, score_id=state.score_id)
        job_ids = [str(track.get("sourceJobId")) for track in (player or {}).get("vocals") or []
                   if track.get("sourceJobId")]
        jobs = await asyncio.to_thread(request.app.state.job_store.get_jobs, job_ids)
        voices: list[str] = []
        base = f"/public/s/{token}"
        for track, _ in songs.usable_vocals(
            player, score_id=state.score_id, score_summary=state.score_summary, jobs_by_id=jobs
        ):
            vocal = songs.vocal_response(track)
            voice = vocal["voice_name"]
            if isinstance(voice, str) and voice and voice not in voices:
                voices.append(voice)
            player_payload["vocals"].append({
                key: vocal[key] for key in (
                    "key", "mode", "label", "verse_number", "duration_seconds",
                    "voice_name", "muted", "solo", "volume"
                )
            } | {"audio_url": f"{base}/audio?{urlencode({'track': vocal['key']})}"})
        summary = state.score_summary if isinstance(state.score_summary, dict) else {}
        performance = summary.get("performance_midi")
        performance = performance if isinstance(performance, dict) else {}
        files = state.files
        midi_key = ("instrumental_midi_expanded_path" if player_payload["repeats"]
                    else "instrumental_midi_original_path")
        midi_flag = ("expanded_midi_available" if player_payload["repeats"]
                     else "original_midi_available")
        midi_available = bool(performance.get("has_instrumental_parts")
                              and performance.get(midi_flag) and files.get(midi_key))
        player_payload = {
            "repeats": player_payload["repeats"],
            "vocals": player_payload["vocals"],
            "instruments": player_payload["instruments"],
            "bus": player_payload["bus"],
            "midi_url": f"{base}/midi?expand_repeats={str(player_payload['repeats']).lower()}"
            if midi_available else None,
        }
        viewer = await optional_user(request)
        is_owner = bool(viewer and viewer[0] == share["ownerUid"])
        if not is_owner:
            await count_share_use(token)
        return {
            "title": state.title or songs.UNTITLED_SONG,
            "info": songs.song_info_response(state.song_info, title=state.title, voices=voices),
            "is_owner": is_owner,
            "song_id": state.id if is_owner else None,
            "score": {
                "version": state.current_score_version,
                "url": f"{base}/score",
                "summary": {
                    **{key: summary.get(key) for key in (
                        "duration_seconds", "expanded_duration_seconds",
                        "repeat_expansion_available", "performance_measure_map"
                    )},
                    "performance_midi": {key: performance.get(key) for key in (
                        "version", "instrumental_parts", "has_instrumental_parts",
                        "original_midi_available", "expanded_midi_available"
                    )},
                },
            },
            "player": player_payload,
            "credits": {"voices": voices},
        }

    @app.get("/s/{token}", response_class=HTMLResponse)
    async def share_link_page(token: str, request: Request) -> HTMLResponse:
        """The app's page with the share link's preview tags, which chat apps read without JavaScript."""
        base = settings.share_base_url
        share_url = share_link_url(settings, token)
        tags = share_preview.generic_tags(share_url=share_url, base_url=base)
        resolved = await resolve_share(token, request, "page")
        if not isinstance(resolved, Response):
            _, state = resolved
            version = await song_thumbnail_version(sessions, settings, state)
            image = f"{base}/public/s/{token}/thumbnail.png?v={version}" if version is not None else None
            tags = share_preview.song_tags(title=state.title, info=state.song_info, share_url=share_url,
                                           image_url=image, base_url=base)
        shell = await share_preview.app_shell(settings.app_shell_url or f"{base}/app")
        page = (share_preview.inject(shell, tags) if shell
                else share_preview.fallback_page(tags, app_url=f"{base}/app"))
        return HTMLResponse(page, headers={"Cache-Control": "no-cache",
                                           "X-Robots-Tag": "noindex, nofollow"})

    @app.get("/public/s/{token}/thumbnail.png")
    async def public_share_thumbnail(token: str, request: Request) -> Response:
        resolved = await resolve_share(token, request, "page")
        if isinstance(resolved, Response):
            return resolved
        _, state = resolved
        version = await song_thumbnail_version(sessions, settings, state)
        data = (await asyncio.to_thread(load_thumbnail, settings, sessions.session_dir(state.id),
                                        state.id, version, "large") if version is not None else None)
        if data is None:
            return share_error(404, "share_off", "share.off")
        return Response(content=data, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/public/s/{token}/score")
    async def public_share_score(token: str, request: Request) -> Response:
        resolved = await resolve_share(token, request, "page")
        if isinstance(resolved, Response):
            return resolved
        _, state = resolved
        return await serve_score(request.app.state.sessions, settings, state.id, state.user_id,
                                  state.snapshot(), touch=False)

    @app.get("/public/s/{token}/midi")
    async def public_share_midi(token: str, request: Request, expand_repeats: bool = False) -> Response:
        resolved = await resolve_share(token, request, "page")
        if isinstance(resolved, Response):
            return resolved
        _, state = resolved
        return await serve_instrumental_midi(
            request.app.state.sessions, state.id, state.user_id, state.snapshot(), expand_repeats,
            midi_signature=midi_signature, restore_midi=restore_midi, touch=False,
        )

    class SharePlayFileResponse(FileResponse):
        """Count the actual response since FileResponse negotiates ranges when sent."""

        def __init__(self, path: Path, token: str):
            super().__init__(path, media_type=audio_media_type(str(path)))
            self.share_token = token

        async def __call__(self, scope, receive, send) -> None:
            async def send_with_counter(message):
                if message["type"] == "http.response.start" and message["status"] == 200:
                    await count_share_use(self.share_token, play=True)
                await send(message)

            await super().__call__(scope, receive, send_with_counter)

    @app.get("/public/s/{token}/audio")
    async def public_share_audio(
        token: str, request: Request, track: str = "", first: str | None = None,
    ) -> Response:
        resolved = await resolve_share(token, request, "audio")
        if isinstance(resolved, Response):
            return resolved
        share, state = resolved
        player = await sessions.get_player(state.id, share["ownerUid"], shared=True)
        job_ids = [str(item.get("sourceJobId")) for item in (player or {}).get("vocals") or []
                   if item.get("sourceJobId")]
        jobs = await asyncio.to_thread(request.app.state.job_store.get_jobs, job_ids)
        usable = ((item, job) for item, job in songs.usable_vocals(
            player, score_id=state.score_id, score_summary=state.score_summary, jobs_by_id=jobs
        ) if item.get("key") == track)
        found = next(usable, None)
        if found is None:
            return share_error(404, "share_track_not_found", "share.track_not_found")
        item, job = found
        job_id = item.get("sourceJobId")
        if job.get("sessionId") != state.id or job.get("userId") != share["ownerUid"]:
            return share_error(404, "share_track_not_found", "share.track_not_found")
        output = job.get("outputPath")
        if settings.backend_use_storage:
            path = songs.owned_job_path(output, user_id=share["ownerUid"],
                                       session_id=state.id, job_id=job_id)
            if path is None:
                return share_error(404, "share_track_not_found", "share.track_not_found")
            audio = await stream_storage_audio(request, settings, path, download=False)
            if first == "1" and audio.status_code == 200:
                await count_share_use(token, play=True)
            return audio
        if not isinstance(output, str):
            return share_error(404, "share_track_not_found", "share.track_not_found")
        path = (settings.project_root / output).resolve()
        if not path.is_relative_to(sessions.session_dir(state.id)) or not path.is_file():
            return share_error(404, "share_track_not_found", "share.track_not_found")
        if first == "1":
            return SharePlayFileResponse(path, token)
        return FileResponse(path, media_type=audio_media_type(str(path)))


def valid_share_token(token: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{22}", token))


def share_link_url(settings: Settings, token: str) -> str:
    return f"{settings.share_base_url}/s/{token}"


THUMBNAIL_RENDER_TIMEOUT_SECONDS = 10.0


async def render_and_store_thumbnail(
    sessions: SessionStore, settings: Settings, session_id: str, version: int, musicxml_path: Path,
) -> bool:
    """Make a Song's score thumbnail and record its version. A failure only leaves it without one."""
    def render() -> tuple[bytes, bytes]:
        content = read_musicxml_content_bounded(
            musicxml_path, max_mxl_uncompressed_bytes=settings.max_mxl_uncompressed_bytes)
        return render_thumbnail(content)

    try:
        large, small = await asyncio.wait_for(asyncio.to_thread(render), THUMBNAIL_RENDER_TIMEOUT_SECONDS)
        await asyncio.to_thread(store_thumbnail, settings, sessions.session_dir(session_id),
                                session_id, version, large, small)
        await sessions.set_thumbnail_version(session_id, version)
        return True
    except Exception:
        get_logger("backend.api").exception(
            "score_thumbnail_failed session_id=%s version=%s", session_id, version)
        return False


async def song_thumbnail_version(sessions: SessionStore, settings: Settings, state: Any) -> Optional[int]:
    """The Song's thumbnail version, rendering it first for a Song from before thumbnails."""
    version = stored_thumbnail_version(state.files)
    if version is not None:
        return version
    try:
        path = await active_score_path(sessions, settings, state.id, state.user_id,
                                        state.snapshot(), touch=False)
    except HTTPException:
        return None
    version = state.current_score_version
    if not await render_and_store_thumbnail(sessions, settings, state.id, version, path):
        return None
    return version


def share_error(status: int, code: str, message_key: str, *, retry_after: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"code": code, "message": backend_message(message_key)},
        headers={"Retry-After": retry_after} if retry_after else None,
    )


async def active_score_path(
    sessions: SessionStore, settings: Settings, session_id: str,
    user_id: str | None, snapshot: Dict[str, Any], *, touch: bool = True,
) -> Path:
    """The session's current MusicXML file, as the score route and the thumbnail read it."""
    # The score's recorded source path may be another backend's disk (the
    # preprocess worker's), so it is never read here: a scored session serves
    # its active artifact, which stays inside this session's folder or comes
    # from storage.
    score_path: Path
    if not current_score_has_source_path(snapshot.get("current_score")):
        files = snapshot.get("files")
        fallback_path = files.get("musicxml_path") if isinstance(files, dict) else None
        if not isinstance(fallback_path, str) or not fallback_path.strip():
            raise HTTPException(status_code=404, detail="Score not found.")
        score_path = resolve_allowlisted_score_path(settings, fallback_path)
    else:
        try:
            score_path = await sessions.ensure_active_musicxml(session_id, user_id, touch=touch)
        except SessionMusicXmlUnavailableError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
    if not score_path.is_file():
        raise HTTPException(status_code=404, detail="Score file not found.")
    return score_path


async def serve_score(
    sessions: SessionStore, settings: Settings, session_id: str,
    user_id: str | None, snapshot: Dict[str, Any], *, touch: bool = True,
) -> Response:
    score_path = await active_score_path(sessions, settings, session_id, user_id, snapshot, touch=touch)
    content = read_musicxml_for_response(
        score_path,
        max_mxl_uncompressed_bytes=settings.max_mxl_uncompressed_bytes,
    )
    # A session keeps one stable score URL while transforms replace the
    # underlying MusicXML.  Prevent browsers and intermediaries from
    # serving the score that was current before the latest transform.
    return Response(
        content=content,
        media_type="application/xml",
        headers={"Cache-Control": "no-store"},
    )


async def serve_instrumental_midi(
    sessions: SessionStore, session_id: str,
    user_id: str | None, snapshot: Dict[str, Any], expand_repeats: bool,
    *, midi_signature: MidiSignature, restore_midi: RestoreMidi, touch: bool = True,
) -> FileResponse:
    files = snapshot.get("files")
    published_signature = (
        files.get(INSTRUMENTAL_MIDI_SIGNATURE_METADATA_KEY) if isinstance(files, dict) else None
    )
    if not isinstance(snapshot.get("current_score"), dict) or not published_signature:
        raise HTTPException(status_code=404, detail="Instrumental MIDI is unavailable for this score.")
    summary = snapshot.get("score_summary")
    try:
        musicxml_path = None if touch else await sessions.ensure_active_musicxml(
            session_id, user_id, touch=False
        )
        current_signature = await midi_signature(
            session_id,
            user_id=user_id,
            score_id=snapshot.get("score_id"),
            score_summary=summary if isinstance(summary, dict) else None,
            musicxml_path=musicxml_path,
        )
    except (KeyError, OSError, SessionMusicXmlUnavailableError) as exc:
        raise HTTPException(
            status_code=404, detail="Instrumental MIDI is unavailable for this score."
        ) from exc
    if published_signature != current_signature:
        raise HTTPException(status_code=404, detail="Instrumental MIDI is unavailable for this score.")
    key = (
        "instrumental_midi_expanded_path"
        if expand_repeats
        else "instrumental_midi_original_path"
    )
    stored_path = files.get(key) if isinstance(files, dict) else None
    if not isinstance(stored_path, str) or not stored_path:
        raise HTTPException(status_code=404, detail="Instrumental MIDI is unavailable for this score.")
    midi_path = session_midi_local_path(sessions, session_id, stored_path)
    # Another instance may have published the file: restore it from storage.
    if not await restore_midi(
        user_id=user_id,
        session_id=session_id,
        files=files,
        kind="expanded" if expand_repeats else "written",
        local_path=midi_path,
    ):
        raise HTTPException(status_code=404, detail="Instrumental MIDI file not found.")
    return FileResponse(
        midi_path,
        media_type="audio/midi",
        filename=midi_path.name if touch else None,
        headers={"Cache-Control": "no-store"},
    )


def session_midi_local_path(sessions: SessionStore, session_id: str, stored_path: str) -> Path:
    """Return this instance's path for a published session MIDI file.

    The recorded path may be another backend's disk (the synthesis worker's),
    so only its file name is used, inside this session's own folder; the file
    is restored from storage when this instance does not have it.
    """
    name = Path(stored_path).name
    if Path(name).suffix.lower() not in {".mid", ".midi"}:
        raise HTTPException(status_code=404, detail="Instrumental MIDI not found.")
    return sessions.session_dir(session_id) / name



def client_ip(request: Request) -> str | None:
    """Return the best-effort client IP for abuse checks behind Cloud Run."""
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or None
    return request.client.host if request.client else None


def audio_media_type(storage_path: str) -> str:
    """Infer the audio media type from a file suffix."""
    suffix = Path(storage_path).suffix.lower()
    if suffix == ".wav":
        return "audio/wav"
    if suffix == ".mp3":
        return "audio/mpeg"
    return "application/octet-stream"


def audio_response_headers(*, download: bool, file_name: str | None) -> dict[str, str]:
    """Build common headers for audio playback/download responses."""
    headers = {"Accept-Ranges": "bytes"}
    if download and file_name:
        headers["Content-Disposition"] = f'attachment; filename="{file_name}"'
    return headers


def parse_byte_range(range_header: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single HTTP byte range."""
    if not range_header:
        return None
    value = range_header.strip()
    if not value.startswith("bytes="):
        raise HTTPException(status_code=416, detail="Invalid Range header.")
    spec = value[6:].strip()
    if "," in spec:
        raise HTTPException(status_code=416, detail="Multiple ranges are not supported.")
    if "-" not in spec:
        raise HTTPException(status_code=416, detail="Invalid Range header.")
    start_text, end_text = spec.split("-", 1)
    if not start_text:
        try:
            suffix_length = int(end_text)
        except ValueError as exc:
            raise HTTPException(status_code=416, detail="Invalid Range header.") from exc
        if suffix_length <= 0:
            raise HTTPException(status_code=416, detail="Invalid Range header.")
        if suffix_length >= size:
            return (0, size - 1)
        return (size - suffix_length, size - 1)
    try:
        start = int(start_text)
    except ValueError as exc:
        raise HTTPException(status_code=416, detail="Invalid Range header.") from exc
    if start < 0 or start >= size:
        raise HTTPException(status_code=416, detail="Requested range not satisfiable.")
    if not end_text:
        return (start, size - 1)
    try:
        end = int(end_text)
    except ValueError as exc:
        raise HTTPException(status_code=416, detail="Invalid Range header.") from exc
    if end < start:
        raise HTTPException(status_code=416, detail="Requested range not satisfiable.")
    return (start, min(end, size - 1))


async def stream_storage_audio(
    request: Request,
    settings: Settings,
    storage_path: str,
    *,
    download: bool = False,
    file_name: str | None = None,
) -> Response:
    """Serve stored audio, reading only the requested bytes from storage, in chunks."""
    media_type = audio_media_type(storage_path)
    bucket = settings.storage_bucket
    size = await asyncio.to_thread(object_size, bucket, storage_path)
    headers = audio_response_headers(download=download, file_name=file_name)
    byte_range = parse_byte_range(request.headers.get("range"), size)
    start, end = byte_range if byte_range is not None else (0, size - 1)
    headers["Content-Length"] = str(end - start + 1)
    status_code = 200
    if byte_range is not None:
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        iter_storage_range(bucket, storage_path, start, end),
        status_code=status_code,
        media_type=media_type,
        headers=headers,
    )


async def iter_storage_range(
    bucket: str, storage_path: str, start: int, end: int
) -> AsyncIterator[bytes]:
    """Yield bytes start..end of a stored object, one bounded download at a time."""
    position = start
    while position <= end:
        chunk_end = min(position + STORAGE_AUDIO_CHUNK_BYTES - 1, end)
        chunk = await asyncio.to_thread(
            download_byte_range, bucket, storage_path, position, chunk_end
        )
        if not chunk:
            return
        yield chunk
        position += len(chunk)


def read_musicxml_for_response(path: Path, *, max_mxl_uncompressed_bytes: int) -> str:
    """Read MusicXML content and map bounded archive failures to HTTP errors."""
    try:
        return read_musicxml_content_bounded(
            path,
            max_mxl_uncompressed_bytes=max_mxl_uncompressed_bytes,
        )
    except MusicXmlArchiveTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except MusicXmlArchiveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def current_score_has_source_path(current_score: Any) -> bool:
    """Return True when the current score records the MusicXML it came from."""
    if not isinstance(current_score, dict):
        return False
    score_payload = current_score.get("score")
    if not isinstance(score_payload, dict):
        return False
    source_musicxml_path = score_payload.get("source_musicxml_path")
    return isinstance(source_musicxml_path, str) and bool(source_musicxml_path.strip())


def resolve_allowlisted_score_path(settings: Settings, candidate: str | Path) -> Path:
    """Resolve a score path and require that it stays within approved backend roots."""
    score_path = Path(candidate)
    if not score_path.is_absolute():
        score_path = settings.project_root / score_path
    resolved = score_path.resolve()
    approved_roots = (
        settings.sessions_dir.resolve(),
        settings.data_dir.resolve(),
    )
    for root in approved_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise HTTPException(status_code=403, detail="Score path is outside allowed roots.")

