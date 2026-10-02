"""API boundary for writing a name into the Happy Birthday demo song.

The name is validated by the synthesis's own phonemizer for the voicebank that
will sing it: whatever synthesis rejects is rejected here, and the number of
vowels it hears is the number of syllables, so every note gets one. The
spelling split ("Hen-ry") comes from the caller; this module checks it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from src.api.score import parse_score
from src.musicxml.birthday_name import MAX_SYLLABLES, NamePlaceholderError, write_birthday_name
from src.phonemizer.phonemizer import UnsupportedLyricTokenError


def replace_birthday_name(
    source_musicxml_path: str | Path,
    output_musicxml_path: str | Path,
    *,
    name: Optional[str],
    sung_text: Optional[str],
    voicebank_path: str | Path,
    selected_verse_number: Optional[str | int] = None,
    selected_lyric_selection: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Validate a name, write it into the placeholder, and return the reparsed score.

    ``name=None`` restores the original "you". Returns ``status: "name_ready"``
    (with ``unchanged: True`` when the score already says exactly this), or an
    ``action_required`` payload naming why the name cannot be sung.
    """
    words: Optional[List[List[str]]] = None
    if name is not None:
        validated = _validate_name(name, sung_text or "", Path(voicebank_path))
        if "status" in validated:
            return validated
        words = validated["words"]
    output_path = Path(output_musicxml_path)
    try:
        result = write_birthday_name(Path(source_musicxml_path), output_path, words=words)
    except NamePlaceholderError as exc:
        return _action_required(
            "demo_placeholder_not_found",
            "This score has no birthday name placeholder to replace.",
            {"detail": str(exc)},
        )
    sung = " ".join("-".join(word) for word in words) if words is not None else "you"
    if result["status"] == "unchanged":
        return {"status": "name_ready", "unchanged": True, "sung_text": sung}
    parsed = parse_score(
        output_path,
        verse_number=selected_verse_number,
        lyric_selection=selected_lyric_selection,
        expand_repeats=False,
    )
    score = dict(parsed)
    summary = score.pop("score_summary", None)
    return {
        "status": "name_ready",
        "unchanged": False,
        "sung_text": sung,
        "derived_score": score,
        "score_summary": summary,
        "derived_musicxml_path": str(output_path.resolve()),
    }


def _validate_name(name: str, sung_text: str, voicebank_path: Path) -> Dict[str, Any]:
    # Imported here: the synthesis module loads model tooling the transforms don't need.
    from src.api.synthesize import _init_phonemizer

    written_words = sung_text.split()
    if not written_words or any(not piece for word in written_words for piece in word.split("-")):
        return _mismatch(sung_text, None, "Write the name as syllables split by hyphens, e.g. Hen-ry.")
    if "".join(written_words).replace("-", "").casefold() != "".join(name.split()).casefold():
        return _mismatch(sung_text, None, "The syllables must spell the name exactly.")

    words: List[List[str]] = []
    vowel_counts: List[int] = []
    for written in written_words:
        pieces = written.split("-")
        plain = "".join(pieces)
        phonemizer = _init_phonemizer(voicebank_path, "en", needed_graphemes=[plain])
        try:
            phonemes = phonemizer.phonemize_tokens([plain]).phonemes
        except UnsupportedLyricTokenError as exc:
            return _action_required(
                "name_not_singable",
                exc.error_message,
                {"word": plain, "reason": exc.reason},
            )
        vowels = sum(1 for phoneme in phonemes if phonemizer.is_vowel(phoneme))
        if vowels == 0:
            return _action_required(
                "name_not_singable",
                f"'{plain}' has no vowel the voice can sing.",
                {"word": plain},
            )
        words.append(pieces)
        vowel_counts.append(vowels)

    total = sum(vowel_counts)
    if total > MAX_SYLLABLES:
        return _action_required(
            "name_too_long",
            f"The name has {total} syllables; the song has room for {MAX_SYLLABLES}. "
            "Ask the user for a shorter name or a nickname.",
            {"syllables": total, "max_syllables": MAX_SYLLABLES},
        )
    for pieces, vowels in zip(words, vowel_counts):
        if len(pieces) != vowels:
            return _mismatch(
                sung_text,
                {"word": "".join(pieces), "expected_syllables": vowels},
                f"Split '{''.join(pieces)}' into exactly {vowels} syllable(s) with hyphens.",
            )
    return {"words": words}


def _mismatch(sung_text: str, diagnostics: Optional[Dict[str, Any]], message: str) -> Dict[str, Any]:
    return _action_required(
        "name_syllables_mismatch",
        message,
        {"sung_text": sung_text, **(diagnostics or {})},
    )


def _action_required(code: str, message: str, diagnostics: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "action_required",
        "action": code,
        "code": code,
        "message": message,
        "diagnostics": diagnostics,
    }
