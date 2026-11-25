"""Build the offline Sing All mixes and a downloadable ZIP for one parent."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ffmpeg
import numpy as np

from src.api import voicebank_cache
from src.backend import sing_all_store, songs
from src.backend.audio_mix import _read_audio
from src.backend.firebase_app import get_firestore_client
from src.backend.sing_all_mix import (
    FULL_INSTRUMENTS_DB, PART_DOMINANT_INSTRUMENTS_DB, Stem, TakeRef, _file_part_name,
    TrackSound, balanced_mix, plan_mixes, render_instruments, write_mp3,
)
from src.backend.storage_client import download_bytes, upload_file


_package_tasks: set[asyncio.Task[None]] = set()


def _song_info_lines(raw: Any, voices: list[str]) -> list[str]:
    """The README's Song info: each filled field, the voices of this zip's takes, the license."""
    info = songs.stored_song_info(raw)
    license_name, license_url = songs.LICENSE_TEXT[info["license"]]
    lines = [f"{label}: {info[key]}" for label, key in (
        ("Subtitle", "subtitle"), ("Composer", "composer"), ("Lyrics by", "lyricist"),
        ("Arranger", "arranger"), ("Credits", "credits"), ("Produced by", "producer"),
    ) if info[key]]
    if voices:
        lines.append(f"Voices: {', '.join(voices)}")
    lines.append(f"License: {license_name}" + (f" ({license_url})" if license_url else ""))
    if info["scoreCopyright"]:
        lines.append(f"Score copyright: {info['scoreCopyright']}")
    if info["description"]:
        lines.extend(["Description:", info["description"]])
    return lines


def _safe_name(value: Any, *, fallback: str = "Song") -> str:
    name = "".join("-" if unicodedata.category(char) == "Cc" else char
                   for char in str(value or ""))
    name = re.sub(r'[\\/:*?"<>|]', "-", name)
    return name.strip(" .")[:80].strip(" .") or fallback


def track_sounds(score_summary: dict) -> list[TrackSound | None]:
    """Use the first browser-default instrument row for each part key."""
    performance = score_summary.get("performance_midi") or {}
    first: dict[str, TrackSound | None] = {}
    sounds: list[TrackSound | None] = []
    for part in performance.get("instrumental_parts") or []:
        if not part.get("eligible"):
            continue
        key = str(part.get("raw_part_id") or part.get("part_index"))
        if key not in first:
            if part.get("program_source") == "user_assignable":
                first[key] = None
            else:
                preset = part.get("playback_preset") or {}
                first[key] = TrackSound(
                    bank=int(preset.get("bank") if preset.get("bank") is not None
                             else part.get("soundfont_bank") if part.get("soundfont_bank") is not None
                             else 128 if part.get("percussion") else 0),
                    program=int(preset.get("program") if preset.get("program") is not None
                                else part.get("midi_program") if part.get("midi_program") is not None
                                else 0),
                )
        sounds.append(first[key])
    return sounds


def _package_write(job_id: str, package: dict[str, Any]) -> None:
    # Replacing the package submap clears stale output fields on a retry while
    # leaving every parent field (including its revision and credits) intact.
    get_firestore_client().collection("singAllJobs").document(job_id).update({"package": package})


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    audio, rate = _read_audio(path)
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    if audio.shape[1] != 2 or rate <= 0:
        raise ValueError("invalid_wav")
    return audio, rate


def _resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return audio
    raw, _ = (ffmpeg.input("pipe:", format="f32le", ac=2, ar=source_rate)
              .output("pipe:", format="f32le", acodec="pcm_f32le", ac=2, ar=target_rate)
              .run(input=audio.astype(np.float32).tobytes(), capture_stdout=True,
                   capture_stderr=True))
    return np.frombuffer(raw, dtype=np.float32).reshape((-1, 2)).copy()


async def _take_wav(child: dict, job: dict, orchestrator: Any, work_dir: Path) -> tuple[Path, bytes]:
    stored = next((value for value in (
        child.get("losslessOutputPath"), child.get("outputPath"))
        if isinstance(value, str) and Path(value).suffix.lower() == ".wav"), None)
    if not isinstance(stored, str) or Path(stored).suffix.lower() != ".wav":
        raise ValueError("missing_wav")
    settings = orchestrator._settings
    if settings.backend_use_storage:
        from src.backend.songs import owned_job_path
        if owned_job_path(stored, user_id=job["uid"], session_id=job["sessionId"],
                          job_id=child["jobId"]) is None:
            raise ValueError("missing_wav")
        data = await asyncio.to_thread(download_bytes, settings.storage_bucket, stored)
    else:
        candidate = Path(stored)
        path = (candidate if candidate.is_absolute() else settings.project_root / candidate).resolve()
        if not path.is_relative_to(orchestrator._sessions.session_dir(job["sessionId"]).resolve()):
            raise ValueError("missing_wav")
        data = path.read_bytes()
    local = work_dir / f"take-{len(list(work_dir.glob('take-*')))}.wav"
    local.write_bytes(data)
    return local, data


async def _midi(job: dict, snapshot: dict, orchestrator: Any) -> Path | None:
    files = snapshot.get("files") or {}
    expanded = bool((job.get("choices") or {}).get("repeats"))
    stored = files.get("instrumental_midi_expanded_path" if expanded
                       else "instrumental_midi_original_path")
    if not isinstance(stored, str) or Path(stored).suffix.lower() not in {".mid", ".midi"}:
        raise ValueError("missing_midi")
    path = orchestrator._sessions.session_dir(job["sessionId"]) / Path(stored).name
    restored = await orchestrator.restore_instrumental_midi(
        user_id=job["uid"], session_id=job["sessionId"], files=files,
        kind="expanded" if expanded else "written", local_path=path)
    if not restored:
        raise ValueError("missing_midi")
    return path


def _credit_file(voice: str, voice_name: str) -> tuple[str, bytes]:
    root = voicebank_cache._local_voicebanks_root()
    voice_dir = (root / voice).resolve()
    if not voice_dir.is_relative_to(root.resolve()) or "/" in voice or "\\" in voice:
        return "character.txt", f"Name: {voice_name}\n".encode()
    if voice_dir.is_dir():
        voice_dir = voicebank_cache.discover_voicebank_root(voice_dir) or voice_dir
    for extension in ("txt", "yaml"):
        path = voice_dir / f"character.{extension}"
        if path.is_file() and path.resolve().is_relative_to(root.resolve()):
            return path.name, path.read_bytes()
    try:
        text_dir = voicebank_cache.resolve_voicebank_text_path(voice)
        text_dir = voicebank_cache.discover_voicebank_root(text_dir) or text_dir
        for extension in ("txt", "yaml"):
            path = text_dir / f"character.{extension}"
            if path.is_file():
                return path.name, path.read_bytes()
    except Exception:
        pass
    return "character.txt", f"Name: {voice_name}\n".encode()


async def run_package(job_id: str, orchestrator: Any) -> None:
    """Build one deterministic package; only the package submap is written."""
    package: dict[str, Any] = {}
    error_code = "package_failed"
    try:
        job = await asyncio.to_thread(sing_all_store.load, job_id)
        if not job:
            return
        takes = [item for item in job.get("items") or []
                 if item.get("type") == "take" and item.get("status") == "done"]
        if not takes:
            now = datetime.now(timezone.utc)
            package = {"state": "failed", "error": "no_takes",
                       "mixes": {"done": 0, "total": 0},
                       "startedAt": now, "updatedAt": now}
            await asyncio.to_thread(_package_write, job_id, package)
            return
        rows = {str(row.get("rowId")): row for row in job.get("rows") or []}
        snapshot = await orchestrator._sessions.get_snapshot(job["sessionId"], job["uid"])
        score = snapshot.get("score_summary") or {}
        sounds = track_sounds(score)
        performance = score.get("performance_midi") or {}
        # The list holds every part, each eligible or not; voices are listed too. Only an
        # eligible part has a MIDI track, as in the player (an a cappella score has none).
        has_instrument_parts = any(part.get("eligible")
                                   for part in performance.get("instrumental_parts") or [])
        title = _safe_name(job.get("title"))
        root = f"{title} - Sing All"
        settings = orchestrator._settings
        base = (f"singAll/{job['uid']}/{job_id}" if settings.backend_use_storage else
                str(orchestrator._sessions.session_dir(job["sessionId"]) / "singAll" / job_id))
        with tempfile.TemporaryDirectory(prefix="sing-all-package-") as tmp:
            work_dir = Path(tmp)
            records: list[tuple[dict, dict, bytes, Stem]] = []
            rate: int | None = None
            for item in takes:
                child = await asyncio.to_thread(orchestrator._job_store.get_job, item["childJobId"])
                if not child:
                    raise ValueError("missing_wav")
                child = {**child, "jobId": item["childJobId"]}
                error_code = "missing_wav"
                local, original = await _take_wav(child, job, orchestrator, work_dir)
                audio, source_rate = await asyncio.to_thread(_read_wav, local)
                if rate is None:
                    rate = source_rate
                audio = await asyncio.to_thread(_resample, audio, source_rate, rate)
                row = rows.get(str(item.get("rowId"))) or {}
                records.append((item, row, original, Stem(str(row.get("name") or item["rowId"]), audio)))
            assert rate is not None
            midi = await _midi(job, snapshot, orchestrator) if has_instrument_parts else None
            instruments = None
            if midi and any(sound is not None for sound in sounds):
                error_code = "render_failed"
                instruments = await asyncio.to_thread(
                    render_instruments, midi, Path(settings.sing_all_soundfont_path), rate, sounds)
            refs = [TakeRef(str(row.get("partId") or item["rowId"]),
                            str(row.get("name") or item["rowId"]), item["mode"])
                    for item, row, _, _ in records]
            planned = plan_mixes(refs, (job.get("choices") or {}).get("mixes") or "none",
                                 any(sound is not None for sound in sounds))
            started = datetime.now(timezone.utc)
            package = {"state": "mixing", "mixes": {"done": 0, "total": len(planned)},
                       "startedAt": started, "updatedAt": started}
            await asyncio.to_thread(_package_write, job_id, package)
            error_code = "mix_failed"
            # The balance between the voices in each mode sung, mixes or not: the Song's
            # player opens each mode's tracks at it.
            balances = []
            for mode in ("lyrics", "solfege"):
                full_vocals = [stem for item, _, _, stem in records if item["mode"] == mode]
                if full_vocals:
                    _, balance = await asyncio.to_thread(
                        balanced_mix, full_vocals, instruments, FULL_INSTRUMENTS_DB, rate)
                    balances.append({"mode": mode, "stats": balance})
            mix_outputs: list[tuple[Any, Path]] = []
            mix_stats: list[dict] = []
            for mix in planned:
                selected = [stem for item, row, _, stem in records
                            if item["mode"] == mix.mode and
                            (mix.kind == "full" or str(row.get("partId") or item["rowId"]) == mix.part_id)]
                if mix.kind == "part":
                    selected = selected[:1]
                audio, stats = await asyncio.to_thread(
                    balanced_mix, selected, instruments,
                    FULL_INSTRUMENTS_DB if mix.kind == "full" else PART_DOMINANT_INSTRUMENTS_DB,
                    rate)
                output = work_dir / mix.file_name
                await asyncio.to_thread(write_mp3, audio, rate, output)
                mix_outputs.append((mix, output))
                mix_stats.append({"file_name": mix.file_name, **stats})
                package["mixes"]["done"] += 1
                package["updatedAt"] = datetime.now(timezone.utc)
                await asyncio.to_thread(_package_write, job_id, package)
            package.update(state="zipping", updatedAt=datetime.now(timezone.utc))
            await asyncio.to_thread(_package_write, job_id, package)
            files: list[dict] = []
            zip_file = work_dir / "package.zip"
            with zipfile.ZipFile(zip_file, "w") as archive:
                def add(name: str, data: bytes, kind: str, mode: str | None = None,
                        part_id: str | None = None, *, stored: bool = False) -> None:
                    archive.writestr(f"{root}/{name}", data,
                                     compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
                    files.append({"name": name, "kind": kind, "mode": mode, "partId": part_id})

                for item, row, original, _ in records:
                    part_id = str(row.get("partId") or item["rowId"])
                    file_mode = "solfège" if item["mode"] == "solfege" else item["mode"]
                    part_name = _file_part_name(str(row.get("name") or part_id), part_id)
                    name = f"1 Solo vocals/{part_name} ({file_mode}).wav"
                    add(name, original, "solo", item["mode"], part_id, stored=True)
                for mix, output in mix_outputs:
                    folder = "3 Full mixes" if mix.kind == "full" else "2 Part-dominant mixes"
                    add(f"{folder}/{mix.file_name}", output.read_bytes(),
                        "full_mix" if mix.kind == "full" else "part_mix",
                        mix.mode, mix.part_id, stored=True)
                if midi:
                    add(f"4 Instrumentals/{title}.mid", midi.read_bytes(), "midi")
                voices: dict[str, str] = {}
                for _, row, _, _ in records:
                    voice = str(row.get("voice") or "")
                    if voice and voice not in voices:
                        voices[voice] = str(row.get("voiceName") or voice)
                for voice, voice_name in sorted(voices.items()):
                    credit_name, credit = await asyncio.to_thread(
                        _credit_file, voice, voice_name)
                    add(f"5 Voicebank credits/{_safe_name(voice_name)}/{credit_name}",
                        credit, "credits")
                readme = ["Sing All package", "1 Solo vocals: original WAV takes",
                          "2 Part-dominant mixes: one voice ahead of instruments",
                          "3 Full mixes: all voices and instruments",
                          "4 Instrumentals: source MIDI", "5 Voicebank credits: voicebank authors",
                          f"Title: {job.get('title') or title}",
                          *_song_info_lines(snapshot.get("song_info"), list(voices.values())),
                          f"Modes: {(job.get('choices') or {}).get('mode', '')}",
                          f"Verse: {(job.get('choices') or {}).get('verse', '')}",
                          "with repeats" if (job.get("choices") or {}).get("repeats") else "as written"]
                readme.extend(f"Take: {row.get('name') or item['rowId']} / "
                              f"{row.get('voiceName') or row.get('voice') or ''} / "
                              f"{'solfège' if item['mode'] == 'solfege' else item['mode']}"
                              for item, row, _, _ in records)
                add("README.txt", ("\n".join(readme) + "\n").encode(), "readme")
            error_code = "upload_failed"
            for mix, output in mix_outputs:
                path = f"{base}/mixes/{mix.file_name}"
                if settings.backend_use_storage:
                    await asyncio.to_thread(upload_file, settings.storage_bucket, output,
                                            path, "audio/mpeg")
                else:
                    target = Path(path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(output.read_bytes())
            zip_path = f"{base}/{root}.zip"
            if settings.backend_use_storage:
                await asyncio.to_thread(upload_file, settings.storage_bucket, zip_file,
                                        zip_path, "application/zip")
            else:
                target = Path(zip_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zip_file.read_bytes())
            package.update(state="done", zipPath=zip_path, files=files,
                           stats={"rate": rate, "mixes": mix_stats, "balances": balances},
                           updatedAt=datetime.now(timezone.utc))
            await asyncio.to_thread(_package_write, job_id, package)
    except Exception as exc:
        if isinstance(exc, ValueError) and str(exc) in {"no_takes", "missing_wav", "missing_midi"}:
            error_code = str(exc)
        package = {"state": "failed", "error": f"{error_code}:{type(exc).__name__}",
                   "mixes": package.get("mixes") or {"done": 0, "total": 0},
                   "startedAt": package.get("startedAt") or datetime.now(timezone.utc),
                   "updatedAt": datetime.now(timezone.utc)}
        await asyncio.to_thread(_package_write, job_id, package)
    finally:
        if package:
            await orchestrator._enqueue_sing_all_reconcile(job_id)


async def start_package(job_id: str, settings: Any) -> None:
    """Start one inline package task or one Cloud Run Job execution."""
    if settings.sing_all_package_transport == "inline":
        from src.backend.main import app
        task = asyncio.create_task(run_package(job_id, app.state.orchestrator))
        _package_tasks.add(task)
        task.add_done_callback(_package_tasks.discard)
        return
    from google.auth import default
    from google.auth.transport.requests import AuthorizedSession

    def start() -> None:
        credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        response = AuthorizedSession(credentials).post(
            f"https://run.googleapis.com/v2/{settings.sing_all_package_job}:run",
            json={"overrides": {"containerOverrides": [{"env": [
                {"name": "SING_ALL_JOB_ID", "value": job_id},
            ]}]}}, timeout=30)
        response.raise_for_status()

    await asyncio.to_thread(start)


async def _main() -> None:
    from src.backend.main import app

    job_id = os.environ["SING_ALL_JOB_ID"]
    async with app.router.lifespan_context(app):
        await run_package(job_id, app.state.orchestrator)


if __name__ == "__main__":
    asyncio.run(_main())
