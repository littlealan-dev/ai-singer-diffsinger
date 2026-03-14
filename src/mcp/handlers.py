from __future__ import annotations

"""MCP tool handlers that bridge JSON-RPC calls to backend APIs."""

import base64
from pathlib import Path
from typing import Any, Dict, Optional

from src.api import (
    get_voicebank_info,
    list_voicebanks,
    parse_score,
    preprocess_voice_parts,
    save_audio,
    synthesize,
)
from src.backend.backing_track import generate_backing_track, generate_combined_backing_track
from src.backend.config import Settings
from src.backend.llm_factory import create_llm_client
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


def handle_reparse(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    """Handle reparse tool calls for direct MCP usage."""
    file_path_param = params.get("file_path")
    if not isinstance(file_path_param, str) or not file_path_param.strip():
        raise ValueError("reparse requires file_path for direct MCP calls.")
    file_path = resolve_project_path(file_path_param)
    return parse_score(
        file_path,
        part_id=params.get("part_id"),
        part_index=params.get("part_index"),
        verse_number=params.get("verse_number"),
        expand_repeats=params.get("expand_repeats", False),
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
    voicebank_path = resolve_voicebank_id(params["voicebank"])
    info = get_voicebank_info(voicebank_path)
    return _strip_path(info)


def handle_generate_backing_track(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    """Handle backing-track generation using deterministic score analysis + ElevenLabs."""
    file_path_param = params.get("file_path")
    if not isinstance(file_path_param, str) or not file_path_param.strip():
        raise ValueError("generate_backing_track requires file_path for backend/direct MCP calls.")
    style_request = str(params.get("style_request") or "").strip()
    if not style_request:
        return {
            "status": "action_required",
            "action": "missing_style_request",
            "code": "missing_style_request",
            "message": "Please describe the backing-track style or genre first.",
        }
    output_path_param = params.get("output_path")
    if not isinstance(output_path_param, str) or not output_path_param.strip():
        raise ValueError("generate_backing_track requires output_path for backend/direct MCP calls.")

    file_path = resolve_project_path(file_path_param)
    output_path = resolve_project_path(output_path_param)
    output_format = str(params.get("output_format") or "").strip() or "mp3_44100_128"
    seed = params.get("seed")
    if seed is not None and not isinstance(seed, int):
        raise ValueError("seed must be an integer when provided.")

    settings = Settings.from_env()
    llm_client = create_llm_client(settings)
    result = generate_backing_track(
        file_path=file_path,
        output_path=output_path,
        style_request=style_request,
        additional_requirements=str(params.get("additional_requirements") or "").strip() or None,
        settings=settings,
        output_format=output_format,
        seed=seed,
        llm_client=llm_client,
    )
    return {
        "status": "completed",
        "output_path": str(output_path.relative_to(settings.project_root)),
        "duration_seconds": result.duration_seconds,
        "output_format": result.output_format,
        "backing_track_prompt": result.prompt,
        "metadata": result.metadata,
        "wav_output_path": (
            str(result.wav_output_path.relative_to(settings.project_root))
            if result.wav_output_path is not None
            else None
        ),
    }


def handle_generate_combined_backing_track(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    """Handle combined melody+backing generation."""
    file_path_param = params.get("file_path")
    if not isinstance(file_path_param, str) or not file_path_param.strip():
        raise ValueError(
            "generate_combined_backing_track requires file_path for backend/direct MCP calls."
        )
    style_request = str(params.get("style_request") or "").strip()
    if not style_request:
        return {
            "status": "action_required",
            "action": "missing_style_request",
            "code": "missing_style_request",
            "message": "Please describe the combined track style or genre first.",
        }
    output_path_param = params.get("output_path")
    if not isinstance(output_path_param, str) or not output_path_param.strip():
        raise ValueError(
            "generate_combined_backing_track requires output_path for backend/direct MCP calls."
        )
    singing_audio_path_param = params.get("singing_audio_path")
    if not isinstance(singing_audio_path_param, str) or not singing_audio_path_param.strip():
        raise ValueError(
            "generate_combined_backing_track requires singing_audio_path for backend/direct MCP calls."
        )

    file_path = resolve_project_path(file_path_param)
    output_path = resolve_project_path(output_path_param)
    singing_audio_path = resolve_project_path(singing_audio_path_param)
    existing_backing_output_path = params.get("existing_backing_output_path")
    if isinstance(existing_backing_output_path, str) and existing_backing_output_path.strip():
        existing_backing_output_path = resolve_project_path(existing_backing_output_path)
    else:
        existing_backing_output_path = None
    existing_backing_wav_path = params.get("existing_backing_wav_path")
    if isinstance(existing_backing_wav_path, str) and existing_backing_wav_path.strip():
        existing_backing_wav_path = resolve_project_path(existing_backing_wav_path)
    else:
        existing_backing_wav_path = None
    output_format = str(params.get("output_format") or "").strip() or "mp3_44100_128"
    seed = params.get("seed")
    if seed is not None and not isinstance(seed, int):
        raise ValueError("seed must be an integer when provided.")

    settings = Settings.from_env()
    llm_client = create_llm_client(settings)
    result = generate_combined_backing_track(
        file_path=file_path,
        output_path=output_path,
        singing_audio_path=singing_audio_path,
        existing_backing_output_path=existing_backing_output_path,
        existing_backing_wav_path=existing_backing_wav_path,
        style_request=style_request,
        additional_requirements=str(params.get("additional_requirements") or "").strip() or None,
        settings=settings,
        output_format=output_format,
        seed=seed,
        llm_client=llm_client,
    )
    return {
        "status": "completed",
        "output_path": str(result.output_path.relative_to(settings.project_root)),
        "duration_seconds": result.duration_seconds,
        "output_format": result.output_format,
        "backing_track_prompt": result.prompt,
        "metadata": result.metadata,
        "backing_track_output_path": str(
            result.backing_track_output_path.relative_to(settings.project_root)
        ),
        "backing_track_wav_output_path": str(
            result.backing_track_wav_output_path.relative_to(settings.project_root)
        ),
        "backing_track_duration_seconds": result.backing_track_duration_seconds,
        "reused_existing_backing_track": result.reused_existing_backing_track,
        "render_variant": "combined",
    }


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
    "preprocess_voice_parts": handle_preprocess_voice_parts,
    "save_audio": handle_save_audio,
    "synthesize": handle_synthesize,
    "generate_backing_track": handle_generate_backing_track,
    "generate_combined_backing_track": handle_generate_combined_backing_track,
    "list_voicebanks": handle_list_voicebanks,
    "get_voicebank_info": handle_get_voicebank_info,
}
