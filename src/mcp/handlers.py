from __future__ import annotations

"""MCP tool handlers that bridge JSON-RPC calls to backend APIs."""

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
    """Return a copy of voicebank info without the filesystem path."""
    info = dict(info)
    info.pop("path", None)
    return info


def handle_parse_score(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    """Handle parse_score tool calls."""
    file_path = resolve_project_path(params["file_path"])
    return parse_score(
        file_path,
        part_id=params.get("part_id"),
        part_index=params.get("part_index"),
        verse_number=params.get("verse_number"),
        expand_repeats=params.get("expand_repeats", False),
    )


def handle_modify_score(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    """Handle modify_score tool calls."""
    return modify_score(params["score"], params["code"])


def handle_save_audio(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    """Handle save_audio tool calls and return base64 audio."""
    output_path = resolve_project_path(params["output_path"])
    result = save_audio(
        params["waveform"],
        output_path,
        sample_rate=params.get("sample_rate", 44100),
        format=params.get("format", "wav"),
        mp3_bitrate=params.get("mp3_bitrate", "256k"),
        keep_wav=bool(params.get("keep_wav", False)),
    )
    # Read the saved audio and return it inline as base64.
    audio_bytes = Path(result["path"]).read_bytes()
    return {
        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
        "duration_seconds": result["duration_seconds"],
        "sample_rate": result["sample_rate"],
    }


def handle_synthesize(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    """Handle synthesize tool calls and wire optional progress updates."""
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
        # File-based progress updates.
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
        # Firestore-backed progress updates.
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
        voice_part_id=params.get("voice_part_id"),
        allow_lyric_propagation=bool(params.get("allow_lyric_propagation", False)),
        source_voice_part_id=params.get("source_voice_part_id"),
        source_part_index=params.get("source_part_index"),
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
    """Resolve the target part index from score metadata."""
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
    """Handle list_voicebanks tool calls."""
    search_path = params.get("search_path")
    resolved_search = resolve_optional_path(search_path)
    voicebanks = list_voicebanks(resolved_search) if resolved_search else list_voicebanks()
    return voicebanks


def handle_get_voicebank_info(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    """Handle get_voicebank_info tool calls."""
    voicebank_path = resolve_voicebank_id(params["voicebank"])
    info = get_voicebank_info(voicebank_path)
    return _strip_path(info)


def handle_estimate_credits(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    """Handle estimate_credits tool calls."""
    score = params["score"]
    uid = params.get("uid")
    email = params.get("email") or ""
    duration_override = params.get("duration_seconds")
    
    # 1. Calculate duration in seconds
    if isinstance(duration_override, (int, float)) and duration_override > 0:
        duration_seconds = float(duration_override)
    else:
        duration_seconds = _calculate_score_duration(score)
    
    # 2. Estimate credits
    from src.backend.credits import estimate_credits, get_or_create_credits
    est_credits = estimate_credits(duration_seconds)
    
    result = {
        "estimated_seconds": round(duration_seconds, 2),
        "estimated_credits": est_credits,
    }
    
    # 3. Add balance info if uid is provided
    if uid:
        try:
            user_credits = get_or_create_credits(uid, email)
            result["current_balance"] = user_credits.balance
            result["balance_after"] = user_credits.balance - est_credits
            result["sufficient"] = (user_credits.available_balance >= est_credits)
            result["overdrafted"] = user_credits.overdrafted
            result["is_expired"] = user_credits.is_expired
        except Exception as e:
            # Logging is handled by the logger in handlers.py
            pass
            
    return result


def _calculate_score_duration(score: Dict[str, Any]) -> float:
    """Calculate the total duration of a score in seconds."""
    tempos = score.get("tempos", [{"offset_beats": 0.0, "bpm": 120.0}])
    parts = score.get("parts", [])
    if not parts:
        return 0.0
        
    # Find the max beat offset across all parts
    max_beats = 0.0
    for part in parts:
        notes = part.get("notes", [])
        if notes:
            last_note = notes[-1]
            max_beats = max(max_beats, last_note["offset_beats"] + last_note["duration_beats"])
            
    if max_beats <= 0:
        return 0.0
        
    # Piecewise linear duration calculation based on tempos
    tempos.sort(key=lambda x: x["offset_beats"])
    
    total_seconds = 0.0
    current_beat = 0.0
    
    for i in range(len(tempos)):
        start_beat = tempos[i]["offset_beats"]
        bpm = tempos[i]["bpm"]
        
        # Determine the end beat for this tempo segment
        if i + 1 < len(tempos):
            end_beat = min(max_beats, tempos[i+1]["offset_beats"])
        else:
            end_beat = max_beats
            
        if end_beat > start_beat:
            segment_beats = end_beat - start_beat
            total_seconds += segment_beats * (60.0 / bpm)
            current_beat = end_beat
            
        if current_beat >= max_beats:
            break
            
    return total_seconds


HANDLERS = {
    "parse_score": handle_parse_score,
    "modify_score": handle_modify_score,
    "save_audio": handle_save_audio,
    "synthesize": handle_synthesize,
    "list_voicebanks": handle_list_voicebanks,
    "get_voicebank_info": handle_get_voicebank_info,
    "estimate_credits": handle_estimate_credits,
}
