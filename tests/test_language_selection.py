from src.backend.language_selection import (
    collect_score_lyrics,
    infer_language_from_text,
    resolve_synthesis_language,
)


def test_explicit_language_wins_over_llm_and_lyrics() -> None:
    resolution = resolve_synthesis_language(
        requested_language="ES",
        proposed_language="en",
        lyric_text="hello world",
        voicebank_id="Keiro",
        voicebank_languages=["en", "es"],
    )

    assert resolution.selected_language == "es"
    assert resolution.source == "user"
    assert resolution.is_supported


def test_language_not_supported_by_voicebank_is_rejected() -> None:
    resolution = resolve_synthesis_language(
        requested_language="es",
        voicebank_id="EnglishBank",
        voicebank_languages=["en"],
    )

    assert not resolution.is_supported
    assert resolution.unsupported_reason == "voicebank_unsupported_language"


def test_non_english_language_requires_declared_voicebank_support() -> None:
    resolution = resolve_synthesis_language(
        requested_language="ja",
        voicebank_id="LegacyBank",
        voicebank_languages=[],
    )

    assert not resolution.is_supported
    assert resolution.unsupported_reason == "voicebank_languages_not_declared"


def test_script_inference_only_handles_unambiguous_scripts() -> None:
    assert infer_language_from_text("こんにちは") == "ja"
    assert infer_language_from_text("你好") == "zh"
    assert infer_language_from_text("สวัสดี") == "th"
    assert infer_language_from_text("hola") is None


def test_collect_score_lyrics_handles_nested_score_data() -> None:
    score = {
        "parts": [
            {"notes": [{"lyric": "hola"}, {"text": "mundo"}]},
            {"lyrics": ["adiós"]},
        ]
    }

    assert collect_score_lyrics(score) == "hola mundo adiós"
