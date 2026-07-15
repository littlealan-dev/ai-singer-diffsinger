"""Deterministic language resolution for synthesis requests."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping, Optional


_LANGUAGE_CODE = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]+)*$")


def normalize_language_code(value: object) -> Optional[str]:
    """Return a normalized BCP-47-style code, or ``None`` when absent/invalid."""
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized if _LANGUAGE_CODE.fullmatch(normalized) else None


def infer_language_from_text(text: str) -> Optional[str]:
    """Infer only unambiguous script-based language hints.

    Latin text is intentionally not classified: it is ambiguous between English,
    Spanish, French, and other languages, so the LLM/user must make that choice.
    """
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\u0e00-\u0e7f]", text):
        return "th"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh"
    return None


def collect_score_lyrics(score: Mapping[str, Any]) -> str:
    """Collect lyric tokens from a parsed score without relying on one schema shape."""
    lyrics: list[str] = []

    def walk(value: object, key: Optional[str] = None) -> None:
        if isinstance(value, Mapping):
            for child_key, child_value in value.items():
                walk(child_value, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif key in {"lyric", "lyrics", "text"} and isinstance(value, str):
            lyrics.append(value)

    walk(score)
    return " ".join(lyrics)


@dataclass(frozen=True)
class ResolvedSynthesisLanguage:
    """The authoritative language decision made before synthesis starts."""

    requested_language: Optional[str]
    proposed_language: Optional[str]
    inferred_language: Optional[str]
    selected_language: str
    source: str
    voicebank_id: str
    available_languages: tuple[str, ...]
    unsupported_reason: Optional[str] = None

    @property
    def is_supported(self) -> bool:
        return self.unsupported_reason is None

    def diagnostics(self) -> dict[str, Any]:
        return {
            "requested_language": self.requested_language,
            "proposed_language": self.proposed_language,
            "inferred_language": self.inferred_language,
            "selected_language": self.selected_language,
            "source": self.source,
            "voicebank": self.voicebank_id,
            "available_languages": list(self.available_languages),
        }


def resolve_synthesis_language(
    *,
    requested_language: object = None,
    proposed_language: object = None,
    lyric_text: str = "",
    voicebank_id: str,
    voicebank_languages: Iterable[object] = (),
) -> ResolvedSynthesisLanguage:
    """Resolve and validate one synthesis language for a selected voicebank."""
    requested = normalize_language_code(requested_language)
    proposed = normalize_language_code(proposed_language)
    inferred = infer_language_from_text(lyric_text)
    available = tuple(
        sorted(
            {
                normalized
                for item in voicebank_languages
                if (normalized := normalize_language_code(item)) is not None
            }
        )
    )
    if requested:
        selected, source = requested, "user"
    elif proposed:
        selected, source = proposed, "llm"
    elif inferred:
        selected, source = inferred, "lyrics"
    else:
        selected, source = "en", "default"

    unsupported_reason: Optional[str] = None
    if available:
        if selected not in available:
            unsupported_reason = "voicebank_unsupported_language"
    elif selected != "en":
        # Empty legacy manifest metadata remains backwards-compatible for English,
        # but it is not a product promise for any other language.
        unsupported_reason = "voicebank_languages_not_declared"

    return ResolvedSynthesisLanguage(
        requested_language=requested,
        proposed_language=proposed,
        inferred_language=inferred,
        selected_language=selected,
        source=source,
        voicebank_id=voicebank_id,
        available_languages=available,
        unsupported_reason=unsupported_reason,
    )
