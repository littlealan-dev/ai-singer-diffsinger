"""Cap how many voicebanks keep their ONNX sessions loaded.

Every voicebank used by a GPU worker keeps its models (and their memory pools)
on the GPU, so each new voice adds to what the next render must fit beside.
Model caches record which voicebank loaded each entry; when a voicebank beyond
the cap is used, the least recently used one's entries are dropped.
"""

import gc
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Dict, MutableMapping, Set, Tuple, Union

from src.mcp.logging_utils import get_logger

logger = get_logger(__name__)

_DEFAULT_MAX_LOADED_VOICES = 2

_lock = threading.Lock()
# Voicebank key -> the (cache, key) entries it loaded, most recently used last.
_voices: "OrderedDict[str, Set[Tuple[int, str]]]" = OrderedDict()
_caches: Dict[int, MutableMapping[str, object]] = {}


def max_loaded_voices() -> int:
    """Return the cap, from SYNTHESIS_MAX_LOADED_VOICES (at least 1)."""
    try:
        value = int(os.getenv("SYNTHESIS_MAX_LOADED_VOICES", _DEFAULT_MAX_LOADED_VOICES))
    except ValueError:
        value = _DEFAULT_MAX_LOADED_VOICES
    return max(1, value)


def use(voicebank: Union[str, Path], cache: MutableMapping[str, object], key: str) -> None:
    """Record that a voicebank uses a cache entry, unloading the oldest voice over the cap.

    Call it before loading the entry, so an evicted voice's memory is released
    before the new one is allocated.
    """
    voice = str(Path(voicebank))
    evicted: Dict[str, int] = {}
    with _lock:
        _caches[id(cache)] = cache
        entries = _voices.pop(voice, set())
        entries.add((id(cache), key))
        _voices[voice] = entries
        limit = max_loaded_voices()
        while len(_voices) > limit:
            old_voice, old_entries = _voices.popitem(last=False)
            still_used = set().union(*_voices.values())
            dropped = 0
            for cache_id, entry_key in old_entries:
                if (cache_id, entry_key) in still_used:
                    continue
                if _caches[cache_id].pop(entry_key, None) is not None:
                    dropped += 1
            evicted[old_voice] = dropped
    if evicted:
        # Release the dropped sessions' GPU memory before the new voice loads.
        gc.collect()
        for old_voice, dropped in evicted.items():
            logger.info(
                "voice_models_unloaded voicebank=%s entries=%s max_loaded_voices=%s",
                Path(old_voice).name,
                dropped,
                max_loaded_voices(),
            )


def reset() -> None:
    """Forget all recorded voices (for tests)."""
    with _lock:
        _voices.clear()
        _caches.clear()
