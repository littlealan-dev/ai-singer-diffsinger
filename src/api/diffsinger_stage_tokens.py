from __future__ import annotations

"""Stage-specific DiffSinger phoneme token encoding."""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence

from src.api.voicebank_cache import (
    get_stage_phoneme_inventory,
    resolve_stage_phoneme_inventory_path,
)


@dataclass(frozen=True)
class StagePhonemeInventory:
    """One stage-local phoneme inventory."""

    stage: str
    path: Path
    symbol_to_id: Dict[str, int]
    unique_id_count: int
    max_id: int


@dataclass(frozen=True)
class DiffSingerStageTokenBundle:
    """Stage-local token sequences for one canonical symbol stream."""

    symbols: List[str]
    root_ids: List[int]
    dur_ids: List[int]
    pitch_ids: List[int]
    variance_ids: List[int]


def load_stage_phoneme_inventory(
    voicebank_path: Path | str,
    stage: str,
) -> StagePhonemeInventory:
    """Load a cached stage-local phoneme inventory."""
    path = resolve_stage_phoneme_inventory_path(voicebank_path, stage)
    symbol_to_id = get_stage_phoneme_inventory(voicebank_path, stage)
    ids = list(symbol_to_id.values())
    return StagePhonemeInventory(
        stage=(stage or "").strip().lower(),
        path=path,
        symbol_to_id=symbol_to_id,
        unique_id_count=len(set(ids)),
        max_id=max(ids) if ids else 0,
    )


def _encode_structural_symbol(
    symbol: str,
    inventory: StagePhonemeInventory,
) -> int | None:
    """Resolve narrow framework fallbacks for SP/AP."""
    if symbol == "SP" and "AP" in inventory.symbol_to_id:
        return int(inventory.symbol_to_id["AP"])
    if symbol == "AP" and "SP" in inventory.symbol_to_id:
        return int(inventory.symbol_to_id["SP"])
    return None


def encode_stage_symbols(
    symbols: Sequence[str],
    inventory: StagePhonemeInventory,
) -> List[int]:
    """Encode one canonical symbol stream for a specific stage."""
    encoded: List[int] = []
    for symbol in symbols:
        if symbol in inventory.symbol_to_id:
            encoded.append(int(inventory.symbol_to_id[symbol]))
            continue
        fallback = _encode_structural_symbol(symbol, inventory)
        if fallback is not None:
            encoded.append(fallback)
            continue
        raise ValueError(
            f"{inventory.stage} phoneme inventory cannot encode symbol {symbol!r}. "
            f"Check {inventory.path}"
        )
    return encoded


def build_stage_token_bundle(
    voicebank_path: Path | str,
    symbols: Sequence[str],
) -> DiffSingerStageTokenBundle:
    """Encode a canonical symbol stream into all current DiffSinger stages."""
    voicebank_root = Path(voicebank_path)
    canonical_symbols = [str(symbol) for symbol in symbols]
    root_inventory = load_stage_phoneme_inventory(voicebank_root, "root")
    dur_inventory = load_stage_phoneme_inventory(voicebank_root, "dur")
    pitch_inventory = load_stage_phoneme_inventory(voicebank_root, "pitch")
    variance_inventory = load_stage_phoneme_inventory(voicebank_root, "variance")
    return DiffSingerStageTokenBundle(
        symbols=canonical_symbols,
        root_ids=encode_stage_symbols(canonical_symbols, root_inventory),
        dur_ids=encode_stage_symbols(canonical_symbols, dur_inventory),
        pitch_ids=encode_stage_symbols(canonical_symbols, pitch_inventory),
        variance_ids=encode_stage_symbols(canonical_symbols, variance_inventory),
    )
