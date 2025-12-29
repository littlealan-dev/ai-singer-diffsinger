from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, Optional

from src.api import (
    align_phonemes_to_notes,
    get_voicebank_info,
    list_voicebanks,
    modify_score,
    parse_score,
    phonemize,
    predict_durations,
    predict_pitch,
    predict_variance,
    save_audio,
    synthesize,
    synthesize_audio,
)
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
        expand_repeats=params.get("expand_repeats", False),
    )


def handle_modify_score(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    return modify_score(params["score"], params["code"])


def handle_phonemize(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    voicebank_path = resolve_voicebank_id(params["voicebank"])
    return phonemize(
        params["lyrics"],
        voicebank_path,
        language=params.get("language", "en"),
    )


def handle_align_phonemes_to_notes(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    voicebank_path = resolve_voicebank_id(params["voicebank"])
    return align_phonemes_to_notes(
        params["score"],
        voicebank_path,
        part_index=params.get("part_index", 0),
        voice_id=params.get("voice_id"),
        include_phonemes=params.get("include_phonemes", False),
    )


def handle_predict_durations(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    voicebank_path = resolve_voicebank_id(params["voicebank"])
    return predict_durations(
        phoneme_ids=params["phoneme_ids"],
        word_boundaries=params["word_boundaries"],
        word_durations=params["word_durations"],
        word_pitches=params["word_pitches"],
        voicebank=voicebank_path,
        language_ids=params.get("language_ids"),
        device=device,
    )


def handle_predict_pitch(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    voicebank_path = resolve_voicebank_id(params["voicebank"])
    return predict_pitch(
        phoneme_ids=params["phoneme_ids"],
        durations=params["durations"],
        note_pitches=params["note_pitches"],
        note_durations=params["note_durations"],
        note_rests=params["note_rests"],
        voicebank=voicebank_path,
        language_ids=params.get("language_ids"),
        encoder_out=params.get("encoder_out"),
        device=device,
    )


def handle_predict_variance(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    voicebank_path = resolve_voicebank_id(params["voicebank"])
    return predict_variance(
        phoneme_ids=params["phoneme_ids"],
        durations=params["durations"],
        f0=params["f0"],
        voicebank=voicebank_path,
        language_ids=params.get("language_ids"),
        encoder_out=params.get("encoder_out"),
        device=device,
    )


def handle_synthesize_audio(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    voicebank_path = resolve_voicebank_id(params["voicebank"])
    vocoder_path = resolve_optional_path(params.get("vocoder_path"))
    return synthesize_audio(
        phoneme_ids=params["phoneme_ids"],
        durations=params["durations"],
        f0=params["f0"],
        voicebank=voicebank_path,
        breathiness=params.get("breathiness"),
        tension=params.get("tension"),
        voicing=params.get("voicing"),
        language_ids=params.get("language_ids"),
        vocoder_path=vocoder_path,
        device=device,
    )


def handle_save_audio(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    output_path = resolve_project_path(params["output_path"])
    result = save_audio(
        params["waveform"],
        output_path,
        sample_rate=params.get("sample_rate", 44100),
        format=params.get("format", "wav"),
    )
    audio_bytes = Path(result["path"]).read_bytes()
    return {
        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
        "duration_seconds": result["duration_seconds"],
        "sample_rate": result["sample_rate"],
    }


def handle_synthesize(params: Dict[str, Any], device: str) -> Dict[str, Any]:
    voicebank_path = resolve_voicebank_id(params["voicebank"])
    return synthesize(
        params["score"],
        voicebank_path,
        part_index=params.get("part_index", 0),
        voice_id=params.get("voice_id"),
        device=device,
    )


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
    "phonemize": handle_phonemize,
    "align_phonemes_to_notes": handle_align_phonemes_to_notes,
    "predict_durations": handle_predict_durations,
    "predict_pitch": handle_predict_pitch,
    "predict_variance": handle_predict_variance,
    "synthesize_audio": handle_synthesize_audio,
    "save_audio": handle_save_audio,
    "synthesize": handle_synthesize,
    "list_voicebanks": handle_list_voicebanks,
    "get_voicebank_info": handle_get_voicebank_info,
}
