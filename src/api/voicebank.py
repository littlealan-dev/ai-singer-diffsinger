"""
Voicebank management APIs.
"""

import logging
import yaml
import numpy as np
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.api.voicebank_cache import (
    discover_voicebank_root,
    get_manifest_voicebank_metadata,
    get_enabled_manifest_voicebanks,
    list_voicebank_ids,
    resolve_manifest_voicebank_id,
    resolve_voicebank_path,
)
from src.mcp.logging_utils import get_logger, summarize_payload

logger = get_logger(__name__)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _shared_vocoder_root() -> Path:
    return _project_root() / "assets" / "vocoders"


def _load_character_data(path: Path) -> Dict[str, Any]:
    """Load character.yaml if present, otherwise return an empty dict."""
    char_file = path / "character.yaml"
    if not char_file.exists():
        return {}
    try:
        data = yaml.safe_load(char_file.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_voice_colors(path: Path) -> List[Dict[str, str]]:
    """Extract subbank color/suffix metadata from character.yaml."""
    data = _load_character_data(path)
    subbanks = data.get("subbanks")
    voice_colors: List[Dict[str, str]] = []
    if isinstance(subbanks, list):
        for entry in subbanks:
            if not isinstance(entry, dict):
                continue
            color = entry.get("color")
            if not color:
                continue
            suffix = entry.get("suffix") or ""
            voice_colors.append({"name": str(color), "suffix": str(suffix)})
    return voice_colors


def _resolve_default_voice_color(voice_colors: List[Dict[str, str]]) -> Optional[str]:
    """Pick a preferred voice color name using common defaults."""
    if not voice_colors:
        return None
    preferred = ("normal", "standard", "default")
    for keyword in preferred:
        for entry in voice_colors:
            name = entry.get("name", "")
            if keyword in name.lower():
                return name
    return voice_colors[0].get("name")


def resolve_default_voice_color(voicebank: Union[str, Path]) -> Optional[str]:
    """Resolve the default voice color name from a voicebank directory."""
    path = Path(voicebank)
    voice_colors = _extract_voice_colors(path)
    return _resolve_default_voice_color(voice_colors)


def resolve_voice_color_suffix(
    voicebank: Union[str, Path],
    voice_color: Optional[str],
) -> Optional[str]:
    """Find the suffix associated with a voice color name."""
    if not voice_color:
        return None
    path = Path(voicebank)
    for entry in _extract_voice_colors(path):
        if entry.get("name") == voice_color:
            suffix = entry.get("suffix")
            return suffix or None
    return None


def resolve_voice_color_speaker(
    voicebank: Union[str, Path],
    voice_color: Optional[str],
) -> Optional[str]:
    """Resolve a speaker suffix that matches a chosen voice color."""
    suffix = resolve_voice_color_suffix(voicebank, voice_color)
    if not suffix:
        return None
    path = Path(voicebank)
    config = load_voicebank_config(path)
    speakers = config.get("speakers", [])
    for entry in speakers:
        if suffix in str(entry):
            return suffix
    return None

def load_voicebank_config(voicebank_path: Union[str, Path]) -> Dict[str, Any]:
    """
    Load the dsconfig.yaml from a voicebank.
    
    Args:
        voicebank_path: Path to voicebank directory
        
    Returns:
        Config dict from dsconfig.yaml
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "load_voicebank_config input=%s",
            summarize_payload({"voicebank_path": str(voicebank_path)}),
        )
    path = Path(voicebank_path)
    config_path = path / "dsconfig.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"dsconfig.yaml not found at {config_path}")
    config = yaml.safe_load(config_path.read_text())
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("load_voicebank_config output=%s", summarize_payload(config))
    return config


def _resolve_vocoder_model_from_dir(vocoder_dir: Path) -> Optional[Path]:
    """Resolve a vocoder model file from a directory or its vocoder.yaml."""
    vocoder_yaml = vocoder_dir / "vocoder.yaml"
    if vocoder_yaml.exists():
        vocoder_conf = yaml.safe_load(vocoder_yaml.read_text())
        model_name = vocoder_conf.get("model")
        if model_name:
            return (vocoder_dir / model_name).resolve()
    fallback = vocoder_dir / "vocoder.onnx"
    if fallback.exists():
        return fallback.resolve()
    return None


def _resolve_shared_vocoder_candidate(vocoder_ref: str) -> Optional[Path]:
    """Resolve a shared vocoder asset by name or model filename."""
    shared_root = _shared_vocoder_root()
    if not shared_root.exists():
        return None

    direct = (shared_root / vocoder_ref).resolve()
    if direct.exists():
        if direct.is_dir():
            return _resolve_vocoder_model_from_dir(direct)
        return direct

    direct_onnx = (shared_root / f"{vocoder_ref}.onnx").resolve()
    if direct_onnx.exists():
        return direct_onnx

    max_depth = 2
    matches: List[Path] = []
    for candidate in shared_root.rglob(f"{vocoder_ref}.onnx"):
        depth = len(candidate.relative_to(shared_root).parts) - 1
        if depth <= max_depth:
            matches.append(candidate)
    if matches:
        matches.sort()
        return matches[0].resolve()
    return None


def resolve_vocoder_model_path(voicebank_path: Union[str, Path]) -> Path:
    """Resolve the actual ONNX vocoder model path for a voicebank."""
    path = Path(voicebank_path)
    conf = load_voicebank_config(path)

    vocoder_ref = conf.get("vocoder")
    if isinstance(vocoder_ref, str) and vocoder_ref:
        direct = (path / vocoder_ref).resolve()
        if direct.exists():
            if direct.is_dir():
                model_path = _resolve_vocoder_model_from_dir(direct)
                if model_path is not None:
                    return model_path
            else:
                return direct

        shared = _resolve_shared_vocoder_candidate(vocoder_ref)
        if shared is not None:
            return shared

    dsvocoder = path / "dsvocoder"
    if dsvocoder.exists():
        model_path = _resolve_vocoder_model_from_dir(dsvocoder)
        if model_path is not None:
            return model_path

    raise FileNotFoundError("Vocoder not found in voicebank or shared vocoder assets.")


def list_voicebanks(search_path: Optional[Union[str, Path]] = None) -> List[Dict[str, Any]]:
    """
    List available voicebanks.
    
    Args:
        search_path: Directory to search (default: assets/voicebanks)
        
    Returns:
        List of voicebank info dicts with:
        - id: Directory name
        - name: Display name from character.yaml
        - path: Relative path (project-root relative when under it)
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "list_voicebanks input=%s",
            summarize_payload({"search_path": str(search_path) if search_path else None}),
        )
    root_dir = _project_root()
    if search_path is None:
        manifest_entries = get_enabled_manifest_voicebanks()
        voicebanks = [
            {
                "id": str(entry["id"]),
                "name": str(entry.get("name") or entry["id"]),
                "path": str(entry["id"]),
            }
            for entry in manifest_entries
            if isinstance(entry.get("id"), str) and entry.get("id")
        ]
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("list_voicebanks output=%s", summarize_payload(voicebanks))
        return voicebanks

    search_path = Path(search_path)
    if not search_path.exists():
        return []

    default_search = (root_dir / "assets" / "voicebanks").resolve()
    if search_path.resolve() == default_search:
        voicebanks = []
        for voicebank_id in list_voicebank_ids():
            resolved_item = resolve_voicebank_path(voicebank_id)
            try:
                relative_path = resolved_item.relative_to(root_dir.resolve())
            except ValueError:
                relative_path = resolved_item
            info = {
                "id": voicebank_id,
                "path": str(relative_path),
            }
            char_data = _load_character_data(resolved_item)
            info["name"] = char_data.get("name", voicebank_id) if char_data else voicebank_id
            voicebanks.append(info)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("list_voicebanks output=%s", summarize_payload(voicebanks))
        return voicebanks

    resolved_root = root_dir.resolve()
    resolved_search = search_path.resolve()
    if resolved_search == resolved_root or resolved_root in resolved_search.parents:
        rel_base = resolved_root
    else:
        rel_base = resolved_search

    voicebanks = []
    for item in search_path.iterdir():
        if item.is_dir():
            discovered_root = discover_voicebank_root(item)
            if discovered_root is None:
                continue
            resolved_item = discovered_root.resolve()
            try:
                relative_path = resolved_item.relative_to(rel_base)
            except ValueError:
                relative_path = resolved_item.relative_to(resolved_search)
            info = {
                "id": item.name,
                "path": str(relative_path),
            }
            # Try to get name from character.yaml.
            char_file = discovered_root / "character.yaml"
            if char_file.exists():
                try:
                    char_data = yaml.safe_load(char_file.read_text())
                    info["name"] = char_data.get("name", item.name)
                except Exception:
                    info["name"] = item.name
            else:
                info["name"] = item.name
            voicebanks.append(info)
    
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("list_voicebanks output=%s", summarize_payload(voicebanks))
    return voicebanks


def get_voicebank_info(voicebank: Union[str, Path]) -> Dict[str, Any]:
    """
    Get detailed information about a voicebank.
    
    Args:
        voicebank: Voicebank path or ID
        
    Returns:
        Capabilities dict with:
        - name: Display name
        - languages: List of supported language codes
        - has_pitch_model: Whether pitch prediction is available
        - has_variance_model: Whether variance prediction is available
        - speakers: List of speaker names/embeddings
        - voice_colors: Available subbank colors (if any)
        - default_voice_color: Default color name (or None)
        - sample_rate: Audio sample rate
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "get_voicebank_info input=%s",
            summarize_payload({"voicebank": str(voicebank)}),
        )
    path = Path(voicebank)
    if isinstance(voicebank, str) and not path.exists() and "/" not in voicebank and "\\" not in voicebank:
        manifest_entry = get_manifest_voicebank_metadata(voicebank)
        if manifest_entry:
            result = {
                "name": manifest_entry.get("name") or voicebank,
                "path": voicebank,
                "languages": manifest_entry.get("languages", []),
                "has_duration_model": manifest_entry.get("has_duration_model", False),
                "has_pitch_model": manifest_entry.get("has_pitch_model", False),
                "has_variance_model": manifest_entry.get("has_variance_model", False),
                "speakers": manifest_entry.get("speakers", []),
                "voice_colors": manifest_entry.get("voice_colors", []),
                "default_voice_color": manifest_entry.get("default_voice_color"),
                "sample_rate": manifest_entry.get("sample_rate", 44100),
                "hop_size": manifest_entry.get("hop_size", 512),
                "use_lang_id": manifest_entry.get("use_lang_id", False),
                "gender": manifest_entry.get("gender"),
                "voice_type": manifest_entry.get("voice_type"),
            }
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("get_voicebank_info output=%s", summarize_payload(result))
            return result
        raise FileNotFoundError(f"Voicebank not declared in manifest: {voicebank}")
    config = load_voicebank_config(path)

    # Check for sub-models.
    has_pitch = (path / "dspitch").exists()
    has_variance = (path / "dsvariance").exists()
    has_duration = (path / "dsdur").exists()
    
    # Get languages.
    languages = []
    if "languages" in config:
        lang_path = path / config["languages"]
        if lang_path.exists():
            lang_data = yaml.safe_load(lang_path.read_text())
            if isinstance(lang_data, dict):
                languages = list(lang_data.keys())
    
    # Get speakers.
    speakers = config.get("speakers", [])
    
    # Get name from character.yaml.
    name = path.name
    char_file = path / "character.yaml"
    if char_file.exists():
        try:
            char_data = yaml.safe_load(char_file.read_text())
            name = char_data.get("name", path.name)
        except Exception:
            pass

    voice_colors = _extract_voice_colors(path)
    default_voice_color = _resolve_default_voice_color(voice_colors)
    manifest_voicebank_id = resolve_manifest_voicebank_id(voicebank)
    manifest_metadata = (
        get_manifest_voicebank_metadata(manifest_voicebank_id)
        if manifest_voicebank_id
        else {}
    )

    result = {
        "name": name,
        "path": str(path.resolve()),
        "languages": languages,
        "has_duration_model": has_duration,
        "has_pitch_model": has_pitch,
        "has_variance_model": has_variance,
        "speakers": speakers,
        "voice_colors": voice_colors,
        "default_voice_color": default_voice_color,
        "sample_rate": config.get("sample_rate", 44100),
        "hop_size": config.get("hop_size", 512),
        "use_lang_id": config.get("use_lang_id", False),
        "gender": manifest_metadata.get("gender"),
        "voice_type": manifest_metadata.get("voice_type"),
    }
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("get_voicebank_info output=%s", summarize_payload(result))
    return result


def load_speaker_embed(
    voicebank: Union[str, Path],
    speaker_name: Optional[str] = None,
) -> Optional[np.ndarray]:
    """
    Load a speaker embedding from a voicebank.
    
    Args:
        voicebank: Voicebank path
        speaker_name: Optional speaker name (uses first if not specified)
        
    Returns:
        Speaker embedding as numpy array, or None for single-speaker banks
        that do not ship external embeddings
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "load_speaker_embed input=%s",
            summarize_payload(
                {
                    "voicebank": str(voicebank),
                    "speaker_name": speaker_name,
                }
            ),
        )
    path = Path(voicebank)
    config = load_voicebank_config(path)
    
    speakers = config.get("speakers", [])
    if not speakers:
        return None
    
    # Select speaker.
    chosen = speakers[0]
    if speaker_name:
        for entry in speakers:
            if speaker_name in str(entry):
                chosen = entry
                break
    
    # Load embedding.
    embed_path = path / chosen
    if not embed_path.exists():
        embed_path = embed_path.with_suffix(".emb")
    if not embed_path.exists():
        raise FileNotFoundError(f"Speaker embedding not found: {embed_path}")
    
    embed = np.frombuffer(embed_path.read_bytes(), dtype=np.float32)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("load_speaker_embed output=%s", summarize_payload(embed))
    return embed
