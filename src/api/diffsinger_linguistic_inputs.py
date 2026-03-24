"""Contract-aware linguistic input builder for DiffSinger ONNX encoders."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import yaml

from src.acoustic.model import LinguisticModel
from src.mcp.logging_utils import get_logger, summarize_payload

logger = get_logger(__name__)


class DiffSingerLinguisticContract(str, Enum):
    """Supported linguistic model input families."""

    TOKENS_ONLY = "tokens_only"
    TOKENS_LANG = "tokens_lang"
    TOKENS_WORD = "tokens_word"
    TOKENS_WORD_LANG = "tokens_word_lang"
    TOKENS_PHDUR = "tokens_phdur"
    TOKENS_PHDUR_LANG = "tokens_phdur_lang"


@dataclass(frozen=True)
class DiffSingerLinguisticFeatures:
    """Normalized feature bundle for linguistic ONNX input construction."""

    phoneme_ids: Sequence[int]
    language_ids: Optional[Sequence[int]] = None
    word_boundaries: Optional[Sequence[int]] = None
    word_durations: Optional[Sequence[int]] = None
    phoneme_durations: Optional[Sequence[int]] = None
    language_map: Optional[Dict[str, int]] = None
    default_language_id: Optional[int] = None
    active_language: Optional[str] = None


def load_language_map(path: Optional[Path]) -> Dict[str, int]:
    """Load a DiffSinger/OpenUtau languages.json map."""
    if path is None:
        return {}
    if not path.exists():
        raise FileNotFoundError(
            f"Languages map not found at {path}. Expected a languages.json from the voicebank."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid languages.json format at {path}.")
    return {str(key): int(value) for key, value in data.items()}


def resolve_default_language_id(
    language_map: Dict[str, int],
    active_language: Optional[str] = None,
) -> int:
    """Resolve a deterministic default language ID from a voicebank language map."""
    if not language_map:
        raise ValueError("Cannot resolve default language ID from an empty language map.")
    if "other" in language_map:
        return int(language_map["other"])
    if active_language:
        normalized = str(active_language).strip()
        if normalized in language_map:
            return int(language_map[normalized])
    return min(int(value) for value in language_map.values())


def classify_linguistic_contract(input_names: Sequence[str]) -> DiffSingerLinguisticContract:
    """Classify the ONNX linguistic model contract from its input names."""
    names = set(input_names)
    if names == {"tokens"}:
        return DiffSingerLinguisticContract.TOKENS_ONLY
    if names == {"tokens", "languages"}:
        return DiffSingerLinguisticContract.TOKENS_LANG
    if names == {"tokens", "word_div", "word_dur"}:
        return DiffSingerLinguisticContract.TOKENS_WORD
    if names == {"tokens", "languages", "word_div", "word_dur"}:
        return DiffSingerLinguisticContract.TOKENS_WORD_LANG
    if names == {"tokens", "ph_dur"}:
        return DiffSingerLinguisticContract.TOKENS_PHDUR
    if names == {"tokens", "languages", "ph_dur"}:
        return DiffSingerLinguisticContract.TOKENS_PHDUR_LANG
    raise ValueError(f"Unsupported linguistic input contract: {sorted(names)}")


def _to_int64_batch(values: Sequence[int]) -> np.ndarray:
    """Convert a 1-D int sequence to [1, n] int64 tensor."""
    return np.array([int(v) for v in values], dtype=np.int64)[None, :]


def _language_contract_required(contract: DiffSingerLinguisticContract) -> bool:
    return contract in {
        DiffSingerLinguisticContract.TOKENS_LANG,
        DiffSingerLinguisticContract.TOKENS_WORD_LANG,
        DiffSingerLinguisticContract.TOKENS_PHDUR_LANG,
    }


def _resolve_language_ids(
    features: DiffSingerLinguisticFeatures,
    *,
    token_count: int,
    use_lang_id: bool,
) -> List[int]:
    """Resolve language IDs, falling back to voicebank defaults when needed."""
    if not use_lang_id:
        return [0] * token_count

    language_map = dict(features.language_map or {})
    explicit = list(features.language_ids or [])
    valid_language_ids = set(language_map.values())

    if explicit:
        if len(explicit) != token_count:
            raise ValueError("language_ids length does not match phoneme count.")
        # Treat all-zero placeholder IDs as missing when zero is not a valid language ID.
        if any(val != 0 for val in explicit) or 0 in valid_language_ids or not language_map:
            return [int(v) for v in explicit]

    default_id = features.default_language_id
    if default_id is None and language_map:
        default_id = resolve_default_language_id(language_map, features.active_language)
    if default_id is not None:
        logger.warning(
            "language_ids_missing_using_default token_count=%s default_language_id=%s active_language=%s",
            token_count,
            default_id,
            features.active_language,
        )
        return [int(default_id)] * token_count
    raise ValueError(
        "Linguistic contract requires language IDs, but none could be resolved from explicit "
        "language_ids or the voicebank languages.json."
    )


def build_linguistic_inputs(
    contract: DiffSingerLinguisticContract,
    features: DiffSingerLinguisticFeatures,
    *,
    use_lang_id: bool,
) -> Dict[str, np.ndarray]:
    """Build the tensor input set required by a linguistic ONNX model."""
    phoneme_ids = [int(v) for v in features.phoneme_ids]
    if not phoneme_ids:
        raise ValueError("phoneme_ids cannot be empty.")

    token_count = len(phoneme_ids)
    inputs: Dict[str, np.ndarray] = {
        "tokens": _to_int64_batch(phoneme_ids),
    }

    if _language_contract_required(contract):
        language_ids = _resolve_language_ids(
            features,
            token_count=token_count,
            use_lang_id=use_lang_id,
        )
        inputs["languages"] = _to_int64_batch(language_ids)

    if contract in {
        DiffSingerLinguisticContract.TOKENS_WORD,
        DiffSingerLinguisticContract.TOKENS_WORD_LANG,
    }:
        word_boundaries = [int(v) for v in (features.word_boundaries or [])]
        word_durations = [int(v) for v in (features.word_durations or [])]
        if not word_boundaries or not word_durations:
            raise ValueError("word_boundaries and word_durations are required by this contract.")
        if sum(word_boundaries) != token_count:
            raise ValueError("word_boundaries do not sum to phoneme count.")
        if len(word_boundaries) != len(word_durations):
            raise ValueError("word_boundaries and word_durations must have the same length.")
        if any(v < 1 for v in word_boundaries):
            raise ValueError("word_boundaries must be >= 1.")
        if any(v < 1 for v in word_durations):
            raise ValueError("word_durations must be >= 1.")
        inputs["word_div"] = _to_int64_batch(word_boundaries)
        inputs["word_dur"] = _to_int64_batch(word_durations)

    if contract in {
        DiffSingerLinguisticContract.TOKENS_PHDUR,
        DiffSingerLinguisticContract.TOKENS_PHDUR_LANG,
    }:
        phoneme_durations = [int(v) for v in (features.phoneme_durations or [])]
        if not phoneme_durations:
            raise ValueError("Linguistic contract requires phoneme durations, but none were provided.")
        if len(phoneme_durations) != token_count:
            raise ValueError("phoneme_durations length does not match phoneme count.")
        inputs["ph_dur"] = _to_int64_batch(phoneme_durations)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "linguistic_inputs keys=%s shapes=%s use_lang_id=%s",
            sorted(inputs.keys()),
            {key: list(value.shape) for key, value in inputs.items()},
            use_lang_id,
        )

    return inputs


def run_linguistic_model(
    model: LinguisticModel,
    features: DiffSingerLinguisticFeatures,
    *,
    use_lang_id: bool,
) -> List[Any]:
    """Run a linguistic model using contract-aware tensor assembly."""
    contract = classify_linguistic_contract(model.input_names)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "linguistic_contract model=%s contract=%s input_names=%s features=%s",
            getattr(model, "model_path", "<mock>"),
            contract.value,
            sorted(model.input_names),
            summarize_payload(
                {
                    "phoneme_count": len(features.phoneme_ids),
                    "word_boundaries": list(features.word_boundaries or []),
                    "word_durations": list(features.word_durations or []),
                    "has_language_ids": features.language_ids is not None,
                    "has_phoneme_durations": features.phoneme_durations is not None,
                    "language_map_keys": sorted((features.language_map or {}).keys()),
                    "default_language_id": features.default_language_id,
                }
            ),
        )
    inputs = build_linguistic_inputs(contract, features, use_lang_id=use_lang_id)
    return model.run(inputs)
