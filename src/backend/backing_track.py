from __future__ import annotations

"""Backing-track prompt writing and ElevenLabs composition helpers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import json
import logging
import copy

from src.api.backing_track_prompt import (
    extract_backing_track_metadata,
)
from src.backend.config import Settings
from src.backend.llm_client import LlmClient
from src.backend.llm_factory import create_llm_client
from src.backend.llm_prompt import PromptBundle
from src.backend.secret_manager import read_secret


_PROMPT_WRITER_SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parent / "config" / "backing_track_prompt_writer_system_prompt.txt"
)


@dataclass(frozen=True)
class BackingTrackGenerationResult:
    """Completed backing-track artifact and prompt details."""

    output_path: Path
    duration_seconds: float
    prompt: str
    metadata: Dict[str, Any]
    output_format: str


def generate_backing_track(
    *,
    file_path: str | Path,
    output_path: str | Path,
    style_request: str,
    additional_requirements: Optional[str],
    settings: Settings,
    output_format: str = "mp3_44100_128",
    seed: Optional[int] = None,
    llm_client: Optional[LlmClient] = None,
    logger: Optional[logging.Logger] = None,
) -> BackingTrackGenerationResult:
    """Generate an instrumental backing track for a score and save it to disk."""
    resolved_file_path = Path(file_path).resolve()
    resolved_output_path = Path(output_path).resolve()
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = extract_backing_track_metadata(resolved_file_path)
    prompt = write_backing_track_prompt(
        metadata=metadata,
        style_request=style_request,
        additional_requirements=additional_requirements,
        settings=settings,
        llm_client=llm_client,
        logger=logger,
    )
    audio_chunks = _compose_with_elevenlabs(
        prompt=prompt,
        music_length_ms=int(round(float(metadata["duration_seconds"]) * 1000.0)),
        output_format=output_format,
        seed=seed,
        settings=settings,
    )
    with resolved_output_path.open("wb") as handle:
        for chunk in audio_chunks:
            handle.write(chunk)
    return BackingTrackGenerationResult(
        output_path=resolved_output_path,
        duration_seconds=float(metadata["duration_seconds"]),
        prompt=prompt,
        metadata=metadata,
        output_format=output_format,
    )


def write_backing_track_prompt(
    *,
    metadata: Dict[str, Any],
    style_request: str,
    additional_requirements: Optional[str],
    settings: Settings,
    llm_client: Optional[LlmClient] = None,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Use the configured LLM to turn score metadata into an ElevenLabs prompt."""
    active_logger = logger or logging.getLogger(__name__)
    client = llm_client or create_llm_client(settings)
    if client is None:
        active_logger.error("backing_track_prompt_writer_error reason=llm_client_unavailable")
        raise RuntimeError("Backing-track prompt writer LLM is unavailable.")

    bundle = PromptBundle(
        static_prompt_text=_load_prompt_writer_system_prompt(),
        dynamic_prompt_text=(
            "Backing Track Metadata:\n"
            f"{json.dumps(_prompt_writer_metadata(metadata), indent=2, sort_keys=True)}\n"
            "End Backing Track Metadata."
        ),
    )
    history = [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "style_request": style_request,
                    "additional_requirements": additional_requirements or "",
                },
                sort_keys=True,
            ),
        }
    ]
    raw_text = client.generate(bundle, history)
    prompt = _parse_prompt_writer_response(raw_text)
    if prompt:
        return prompt
    active_logger.error(
        "backing_track_prompt_writer_error reason=invalid_llm_response response=%s",
        raw_text,
    )
    raise RuntimeError("Backing-track prompt writer returned an invalid response.")


def _compose_with_elevenlabs(
    *,
    prompt: str,
    music_length_ms: int,
    output_format: str,
    seed: Optional[int],
    settings: Settings,
):
    """Call the ElevenLabs Music API and return the streaming audio iterator."""
    from elevenlabs.client import ElevenLabs

    api_key = _resolve_elevenlabs_api_key(settings)
    if not api_key:
        raise RuntimeError("ElevenLabs API key is not configured.")
    client = ElevenLabs(api_key=api_key)
    kwargs: Dict[str, Any] = {
        "prompt": prompt,
        "music_length_ms": music_length_ms,
        "model_id": settings.elevenlabs_music_model,
        "force_instrumental": True,
        "output_format": output_format,
    }
    if seed is not None:
        kwargs["seed"] = seed
    return client.music.compose(**kwargs)


def _resolve_elevenlabs_api_key(settings: Settings) -> str:
    """Load the ElevenLabs API key using the standard env/secret pattern."""
    app_env = settings.app_env.lower()
    if app_env in {"dev", "development", "local", "test"}:
        return settings.elevenlabs_api_key
    return read_secret(
        settings,
        settings.elevenlabs_api_key_secret,
        settings.elevenlabs_api_key_secret_version,
    )


def _load_prompt_writer_system_prompt() -> str:
    """Load the backing-track prompt writer system prompt from disk."""
    return _PROMPT_WRITER_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _prompt_writer_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize metadata for the prompt-writer LLM.

    The prompt writer should treat measure 1 as a normal bar and should not receive
    pickup-specific cues. If measure 1 lacks an explicit harmony, reuse the first
    available chord as the starting harmony so the writer can describe a full-bar
    accompaniment from the beginning.
    """
    sanitized = copy.deepcopy(metadata)
    sanitized.pop("pickup", None)
    progression = sanitized.get("chord_progression")
    if not isinstance(progression, list):
        return sanitized
    first_chord = None
    has_measure_one = False
    for entry in progression:
        if not isinstance(entry, dict):
            continue
        measure = entry.get("measure")
        chords = entry.get("chords")
        if measure == 1:
            has_measure_one = True
        if first_chord is None and isinstance(chords, list) and chords:
            candidate = str(chords[0]).strip()
            if candidate:
                first_chord = candidate
    if not has_measure_one and first_chord:
        sanitized["chord_progression"] = [{"measure": 1, "chords": [first_chord]}, *progression]
    return sanitized


def _parse_prompt_writer_response(text: str) -> Optional[str]:
    """Extract the JSON prompt field from an LLM response."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        lines = cleaned.splitlines()
        if lines and lines[0].strip().startswith("json"):
            lines = lines[1:]
        cleaned = "\n".join(lines).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        return None
    prompt = prompt.strip()
    return prompt or None
