from __future__ import annotations

from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSETS_ROOT = PROJECT_ROOT / "assets"
VOICEBANKS_ROOT = ASSETS_ROOT / "voicebanks"


def resolve_project_path(path_value: str) -> Path:
    if not path_value:
        raise ValueError("Path is required.")
    path = Path(path_value)
    if path.is_absolute():
        raise ValueError("Absolute paths are not allowed.")
    resolved = (PROJECT_ROOT / path).resolve()
    if resolved != PROJECT_ROOT and PROJECT_ROOT not in resolved.parents:
        raise ValueError("Path escapes project root.")
    return resolved


def resolve_voicebank_id(voicebank_id: str) -> Path:
    if not voicebank_id:
        raise ValueError("voicebank is required.")
    if "/" in voicebank_id or "\\" in voicebank_id:
        raise ValueError("voicebank must be an ID (directory name).")
    candidate = (VOICEBANKS_ROOT / voicebank_id).resolve()
    if VOICEBANKS_ROOT not in candidate.parents:
        raise ValueError("voicebank ID resolves outside voicebank root.")
    config_path = candidate / "dsconfig.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Voicebank not found: {voicebank_id}")
    return candidate


def resolve_optional_path(path_value: Optional[str]) -> Optional[Path]:
    if path_value is None:
        return None
    return resolve_project_path(path_value)
