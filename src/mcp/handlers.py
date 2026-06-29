from __future__ import annotations

"""MCP tool handlers that bridge JSON-RPC calls to backend APIs."""

import base64
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from src.api import (
    add_solfege_lyric_verse,
    get_voicebank_info,
    list_voicebanks,
    parse_score,
    preprocess_voice_parts,
    save_audio,
    synthesize,
    modify_solfege_settings,
)
from src.backend.progress import write_progress
from src.backend.job_store import JobStore
from src.backend.firebase_app import initialize_firebase_app
from src.api.voicebank_cache import get_manifest_voicebank_metadata
from src.mcp.resolve import resolve_optional_path, resolve_project_path, resolve_voicebank_id


class InvalidMusicXmlError(ValueError):
    """Raised when parse_score cannot parse the supplied score artifact."""

    code = "invalid_musicxml"
    retryable = False
    user_message = (
        "Uploaded file is not valid MusicXML. Please export a MusicXML score and try again."
    )


def _strip_path(info: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of voicebank info without the filesystem path."""
    info = dict(info)
    info.pop("path", None)
    return info


def handle_parse_score(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    """Handle parse_score tool calls."""
    file_path = resolve_project_path(params["file_path"])
    return _parse_musicxml(file_path, params)


def handle_reparse(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    """Handle reparse tool calls for direct MCP usage."""
    file_path_param = params.get("file_path")
    if not isinstance(file_path_param, str) or not file_path_param.strip():
        raise ValueError("reparse requires file_path for direct MCP calls.")
    file_path = resolve_project_path(file_path_param)
    return _parse_musicxml(file_path, params)


def _parse_musicxml(file_path: Path, params: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize every parser exception to one non-retryable tool error."""
    try:
        return parse_score(
            file_path,
            part_id=params.get("part_id"),
            part_index=params.get("part_index"),
            verse_number=params.get("verse_number"),
            expand_repeats=params.get("expand_repeats", False),
        )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "parse_score_invalid_musicxml path=%s error_type=%s error=%s",
            file_path,
            type(exc).__name__,
            exc,
        )
        raise InvalidMusicXmlError(InvalidMusicXmlError.user_message) from exc


def handle_add_solfege_lyric_verse(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    """Handle deterministic solfege verse generation."""
    source_path = resolve_project_path(params["source_musicxml_path"])
    output_path = resolve_project_path(params["output_musicxml_path"])
    return add_solfege_lyric_verse(
        source_path,
        output_path,
        part_id=params.get("part_id"),
        part_index=params.get("part_index"),
        settings=params.get("settings"),
    )


def handle_modify_solfege_settings(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    """Handle rewriting every generated solfege verse."""
    source_path = resolve_project_path(params["source_musicxml_path"])
    output_path = resolve_project_path(params["output_musicxml_path"])
    settings = dict(params.get("settings") or {})
    if params.get("system") is not None:
        settings["system"] = params["system"]
    if params.get("mode") is not None:
        settings["mode"] = params["mode"]
    return modify_solfege_settings(
        source_path,
        output_path,
        settings=settings,
        selected_verse_number=params.get("selected_verse_number"),
    )


def handle_preprocess_voice_parts(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    """Handle preprocess_voice_parts tool calls."""
    score = params.get("score")
    if not isinstance(score, dict):
        raise ValueError("score is required and must be an object")
    request = params.get("request")
    if not isinstance(request, dict):
        return {
            "status": "action_required",
            "action": "preprocessing_plan_required",
            "code": "preprocessing_plan_required",
            "message": "preprocess_voice_parts requires request.plan as an object.",
        }

    if "voice_id" in params or "voice_id" in request:
        return {
            "status": "action_required",
            "action": "deprecated_voice_id_input",
            "code": "deprecated_voice_id_input",
            "message": (
                "Deprecated voice_id is not accepted for preprocess_voice_parts. "
                "Use request.plan.targets[].target.voice_part_id."
            ),
        }

    plan = request.get("plan")
    if not isinstance(plan, dict):
        return {
            "status": "action_required",
            "action": "preprocessing_plan_required",
            "code": "preprocessing_plan_required",
            "message": "preprocess_voice_parts requires request.plan as an object.",
        }

    return preprocess_voice_parts(score, request={"plan": plan})


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
    voicebank_id = params["voicebank"]
    voicebank_metadata = get_manifest_voicebank_metadata(voicebank_id)
    pitch_expression = float(voicebank_metadata.get("pitch_expression", 1.0))
    voicebank_path = resolve_voicebank_id(voicebank_id)
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

    result = synthesize(
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
        pitch_expression=pitch_expression,
        solfege_pronunciation_patch=params.get(
            "solfege_pronunciation_patch", False
        ),
        device=device,
        progress_callback=progress_callback,
    )
    waveform = result.get("waveform")
    if hasattr(waveform, "tolist"):
        result = dict(result)
        result["waveform"] = waveform.tolist()
    return result


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
    info = get_voicebank_info(params["voicebank"])
    return _strip_path(info)


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
    "reparse": handle_reparse,
    "add_solfege_lyric_verse": handle_add_solfege_lyric_verse,
    "modify_solfege_settings": handle_modify_solfege_settings,
    "preprocess_voice_parts": handle_preprocess_voice_parts,
    "save_audio": handle_save_audio,
    "synthesize": handle_synthesize,
    "list_voicebanks": handle_list_voicebanks,
    "get_voicebank_info": handle_get_voicebank_info,
}
