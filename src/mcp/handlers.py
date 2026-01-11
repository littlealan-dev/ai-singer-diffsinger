from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, Optional

from src.api import (
    get_voicebank_info,
    list_voicebanks,
    modify_score,
    parse_score,
    save_audio,
    synthesize,
)
from src.backend.progress import write_progress
from src.backend.job_store import JobStore
from src.backend.firebase_app import initialize_firebase_app
from src.mcp.resolve import resolve_optional_path, resolve_project_path, resolve_voicebank_id


def _strip_path(info: Dict[str, Any]) -> Dict[str, Any]:
    info = dict(info)
    info.pop("path", None)
    return info


def handle_parse_score(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    file_path = resolve_project_path(params["file_path"])
    return parse_score(
        file_path,
        part_id=params.get("part_id"),
        part_index=params.get("part_index"),
        verse_number=params.get("verse_number"),
        expand_repeats=params.get("expand_repeats", False),
    )


def handle_modify_score(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    return modify_score(params["score"], params["code"])


def handle_save_audio(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    output_path = resolve_project_path(params["output_path"])
    result = save_audio(
        params["waveform"],
        output_path,
        sample_rate=params.get("sample_rate", 44100),
        format=params.get("format", "wav"),
        mp3_bitrate=params.get("mp3_bitrate", "256k"),
        keep_wav=bool(params.get("keep_wav", False)),
    )
    audio_bytes = Path(result["path"]).read_bytes()
    return {
        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
        "duration_seconds": result["duration_seconds"],
        "sample_rate": result["sample_rate"],
    }


def handle_synthesize(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    voicebank_path = resolve_voicebank_id(params["voicebank"])
    part_index = _resolve_part_index(
        params.get("score", {}),
        part_id=params.get("part_id"),
        part_index=params.get("part_index"),
    )
    progress_path = params.get("progress_path")
    progress_job_id = params.get("progress_job_id")
    progress_user_id = params.get("progress_user_id")
    progress_callback = None
    if progress_path:
        resolved_progress = resolve_project_path(progress_path)

        def progress_callback(step: str, message: str, progress: float) -> None:
            write_progress(
                resolved_progress,
                {
                    "status": "running",
                    "step": step,
                    "message": message,
                    "progress": progress,
                    "job_id": progress_job_id,
                },
                expected_job_id=progress_job_id,
            )
    elif progress_job_id:
        initialize_firebase_app()
        job_store = JobStore()

        def progress_callback(step: str, message: str, progress: float) -> None:
            job_store.update_job(
                progress_job_id,
                status="running",
                step=step,
                message=message,
                progress=progress,
                userId=progress_user_id,
            )

    return synthesize(
        params["score"],
        voicebank_path,
        part_index=part_index,
        voice_id=params.get("voice_id"),
        voice_color=params.get("voice_color"),
        articulation=params.get("articulation", 0.0),
        airiness=params.get("airiness", 1.0),
        intensity=params.get("intensity", 0.5),
        clarity=params.get("clarity", 1.0),
        device=device,
        progress_callback=progress_callback,
    )


def _resolve_part_index(
    score: Dict[str, Any],
    *,
    part_id: Optional[str],
    part_index: Optional[int],
) -> int:
    if part_id is not None and part_index is not None:
        raise ValueError("Provide part_id or part_index, not both.")
    parts = score.get("parts") or []
    if part_id is not None:
        for idx, part in enumerate(parts):
            if part.get("part_id") == part_id:
                return idx
        raise ValueError(f"part_id not found in score: {part_id}")
    if part_index is not None:
        return part_index
    for idx, part in enumerate(parts):
        notes = part.get("notes") or []
        if any(note.get("lyric") for note in notes):
            return idx
    return 0


def handle_list_voicebanks(params: Dict[str, Any], device: str) -> Any:
    search_path = params.get("search_path")
    resolved_search = resolve_optional_path(search_path)
    voicebanks = list_voicebanks(resolved_search) if resolved_search else list_voicebanks()
    return voicebanks


def handle_get_voicebank_info(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    voicebank_path = resolve_voicebank_id(params["voicebank"])
    info = get_voicebank_info(voicebank_path)
    return _strip_path(info)


HANDLERS = {
    "parse_score": handle_parse_score,
    "modify_score": handle_modify_score,
    "save_audio": handle_save_audio,
    "synthesize": handle_synthesize,
    "list_voicebanks": handle_list_voicebanks,
    "get_voicebank_info": handle_get_voicebank_info,
}
