"""Pure Sing All quote and card-opening helpers."""

from typing import Any, Mapping, Sequence

from src.backend.language_selection import normalize_language_code


def supported_singing_languages(voicebanks: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the languages actually sung by enabled voicebanks."""
    supported: set[str] = set()
    for voicebank in voicebanks:
        declared = voicebank.get("languages") or ["en"]
        if isinstance(declared, list):
            supported.update(
                code for value in declared
                if (code := normalize_language_code(value)) is not None
            )
    return sorted(supported)


def open_sing_all_result(
    arguments: Mapping[str, Any], *, voicebanks: Sequence[Mapping[str, Any]],
    active_job_id: str | None = None,
) -> dict[str, Any]:
    """Validate the chat model's language before opening the card."""
    language = normalize_language_code(arguments.get("language"))
    supported = supported_singing_languages(voicebanks)
    solfege_only = arguments.get("solfegeOnly", False) is True
    reason: str | None = None
    if active_job_id:
        reason = "sing_all_already_running"
    elif language is None:
        reason = "language_required"
    elif language not in supported:
        reason = "no_voicebank_for_language"
    elif solfege_only and language != "en":
        reason = "solfege_only_needs_en"
    if reason is not None:
        return {
            "status": "action_required",
            "reason": reason,
            **({"language": language, "supported": supported}
               if reason == "no_voicebank_for_language" else {}),
        }
    return {
        "status": "ready",
        "singAll": {"language": language, "solfegeOnly": solfege_only},
    }
